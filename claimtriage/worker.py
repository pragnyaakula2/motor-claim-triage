"""Queue workers. One image, two roles (WORKER_ROLE):

  triage    - consumes stream:intake,   runs the Triage Agent, hands off to stream:coverage
  coverage  - consumes stream:coverage, runs the Coverage Agent + verification + routing, writes the final result

The two roles are separate processes/containers: they share nothing but Redis (typed hand-off stored on the run record).
Failure semantics (A2): a message is acknowledged only after its stage finished. If a worker dies mid-run the message stays
pending, another worker reclaims it (XAUTOCLAIM) and re-runs that stage. After MAX_DELIVERIES the run is closed as an
escalation (AGENT_FAILURE) with whatever earlier stages produced - never as a confident answer.
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import metrics as M
from .agents.router import route_claim
from .agents.tools import ToolBox
from .agents.coverage import CoverageAgent
from .agents.triage import TriageAgent
from .config import get_settings
from .contracts import FNOL
from .llm.client import LLMClient
from .llm.prompts import load_prompt
from .logging_setup import agent_var, correlation_id, log, run_id_var, setup_logging
from .store import (GROUP_COVERAGE, GROUP_INTAKE, STREAM_COVERAGE, STREAM_INTAKE, Store, claim_fingerprint)
from .telemetry import extract_context, inject_context, setup_tracing, span
from .trace import TraceRecorder
from . import quality

logger = logging.getLogger("worker")
MAX_DELIVERIES = 3
TERMINAL = {"completed", "escalated", "failed"}


def prompt_versions(s) -> dict:
    return {"triage": load_prompt("triage", s.prompt_triage).id, "coverage": load_prompt("coverage", s.prompt_coverage).id}


class Worker:
    def __init__(self):
        self.s = get_settings()
        self.store = Store()
        self.llm = LLMClient(self.s)
        self.role = self.s.worker_role
        self.stream, self.group = (STREAM_INTAKE, GROUP_INTAKE) if self.role == "triage" else (STREAM_COVERAGE, GROUP_COVERAGE)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=self.s.worker_concurrency)
        self.slots = threading.Semaphore(self.s.worker_concurrency)
        self.in_flight = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        self.store.ensure_groups()
        log(logger, logging.INFO, "worker started", role=self.role, consumer=self.consumer, concurrency=self.s.worker_concurrency)
        last_claim = 0.0
        while not self.stop.is_set():
            try:
                free = self.s.worker_concurrency - self.in_flight
                if free <= 0:
                    time.sleep(0.05)
                    continue
                msgs = []
                if time.time() - last_claim > 5:
                    last_claim = time.time()
                    msgs += self._reclaim(free)
                if len(msgs) < free:
                    resp = self.store.r.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=free - len(msgs), block=1000)
                    for _, entries in resp or []:
                        msgs += entries
                for mid, fields in msgs:
                    self.slots.acquire()
                    with self._lock:
                        self.in_flight += 1
                        M.IN_FLIGHT.labels(self.role).set(self.in_flight)
                    self.pool.submit(self._guarded, mid, fields)
            except Exception as e:  # redis hiccup: back off, never die silently
                log(logger, logging.ERROR, "worker loop error", error=repr(e))
                time.sleep(1)

    def _reclaim(self, n: int) -> list:
        try:
            res = self.store.r.xautoclaim(self.stream, self.group, self.consumer, min_idle_time=self.s.stream_claim_idle_ms, start_id="0-0", count=n)
            return res[1] if res and len(res) > 1 else []
        except Exception:
            return []

    def _guarded(self, mid: str, fields: dict) -> None:
        run_id = fields.get("run_id", "?")
        try:
            delivered = 1
            try:
                pend = self.store.r.xpending_range(self.stream, self.group, mid, mid, 1)
                delivered = pend[0]["times_delivered"] if pend else 1
            except Exception:
                pass
            if delivered > MAX_DELIVERIES:
                self._fail_run(run_id, f"stage '{self.role}' failed {delivered - 1} times")
                self.store.r.xack(self.stream, self.group, mid)
                return
            run = self.store.get_run(run_id)
            if run is None or run["status"] in TERMINAL:
                self.store.r.xack(self.stream, self.group, mid)
                return
            wait = time.time() - float(fields.get("enqueued_at", time.time()))
            M.QUEUE_WAIT.labels(self.stream).observe(wait)
            corr = run["meta"].get("correlation_id", "-")
            tc, tr, ta = correlation_id.set(corr), run_id_var.set(run_id), agent_var.set(self.role)
            try:
                from opentelemetry import context as otctx
                token = otctx.attach(extract_context(json.loads(fields.get("otel", "{}"))))
                try:
                    with span(f"worker.{self.role}", queue_wait_s=round(wait, 3), delivery=delivered):
                        (self._triage if self.role == "triage" else self._coverage)(run, delivered)
                finally:
                    otctx.detach(token)
            finally:
                correlation_id.reset(tc); run_id_var.reset(tr); agent_var.reset(ta)
            self.store.r.xack(self.stream, self.group, mid)
        except Exception as e:
            log(logger, logging.ERROR, "stage crashed; message left pending for redelivery", run_id=run_id, error=repr(e), exc_info=True)
            M.RETRIES.labels(self.role, "redelivery").inc()
        finally:
            with self._lock:
                self.in_flight -= 1
                M.IN_FLIGHT.labels(self.role).set(self.in_flight)
            self.slots.release()

    # ------------------------------------------------------------------ stages
    def _recorder(self, run_id: str) -> TraceRecorder:
        return TraceRecorder(run_id, sink=lambda ev: self.store.append_event(run_id, ev))

    def _triage(self, run: dict, delivery: int) -> None:
        rid = run["run_id"]
        rec = self._recorder(rid)
        if delivery > 1:
            rec.emit("triage", "resume", "redelivery", delivery=delivery)
        self.store.update_run(rid, status="triaging")
        claim, meta = run["claim"], run["meta"]
        handoff = TriageAgent(self.llm, self.s, rec).run(claim, meta["sanitized"], meta.get("security_flags", []))
        self.store.update_run(rid, handoff=handoff, status="analyzing")
        self.store.enqueue(STREAM_COVERAGE, rid, inject_context())

    def _coverage(self, run: dict, delivery: int) -> None:
        rid = run["run_id"]
        rec = self._recorder(rid)
        if delivery > 1:
            rec.emit("coverage", "resume", "redelivery", delivery=delivery)
        claim, meta, handoff = run["claim"], run["meta"], run["handoff"]
        age = time.time() - run["submitted_at"]
        if age > self.s.run_deadline_s:
            rec.emit("coverage", "guard", "run_deadline", age_s=round(age, 1))
            out = {"kind": "escalation", "code": "RUN_TIMEOUT", "detail": f"run exceeded {self.s.run_deadline_s:.0f}s before the coverage stage started",
                   "partial": {"evidence": [], "queries": []}, "degraded": False, "steps": 0, "tool_calls": 0, "attempts": 0}
            M.ESCALATIONS.labels("RUN_TIMEOUT").inc()
            tools = None
        else:
            tools = ToolBox(self.s, claim, rec, self.store)
            out = CoverageAgent(self.llm, self.s, rec, tools, claim, meta["sanitized"], handoff, started_at=run["submitted_at"]).run()
        result = self._assemble(run, out, rec, tools)
        self._finish(run, result, out, tools)

    # ------------------------------------------------------------------ result assembly
    def _assemble(self, run: dict, out: dict, rec: TraceRecorder, tools) -> dict:
        rid, claim, handoff, meta = run["run_id"], run["claim"], run["handoff"], run["meta"]
        events = self.store.get_events(rid)
        totals = TraceRecorder(rid)
        totals.events = events
        tot = totals.totals()
        e2e = time.time() - run["submitted_at"]
        agent_lat: dict[str, float] = {}
        for e in events:
            if e["type"] in ("llm_call", "tool_call", "verification"):
                agent_lat[e["agent"]] = round(agent_lat.get(e["agent"], 0) + e.get("latency_s", 0), 3)
        base = {"run_id": rid, "security_flags": meta.get("security_flags", []), "prompt_versions": meta["prompt_versions"],
                "cost_usd": tot["cost_usd"], "tokens": {k: tot[k] for k in ("prompt_tokens", "completion_tokens", "llm_calls", "tool_calls")} | {"by_step": tot["by_step"]},
                "timings": {"end_to_end_s": round(e2e, 3), "busy_s_by_agent": agent_lat, "steps": out.get("steps"), "verify_attempts": out.get("attempts")}}
        if out["kind"] == "answer":
            ans = out["answer"]
            r = route_claim(claim, handoff, ans["position"], self.s)
            M.ROUTES.labels(r["route"]).inc()
            rec.emit("coverage", "route", "route", **r)
            rec_ = {"claim_type": handoff["claim_type"], "coverage_position": ans["position"], "summary": ans["summary"], "route": r["route"],
                    "route_reason": r["reason"], "within_permitted_use": ans.get("within_permitted_use", "not_applicable"),
                    "citations": ans["citations"], "confidence": ans.get("confidence", "medium") if ans.get("confidence") in ("low", "medium", "high") else "medium"}
            return {**base, "status": "completed", "degraded": False, "recommendation": rec_, "escalation": {"to_human": True,
                    "reason_code": None, "detail": "Advisory output: a human claims handler makes the decision.",
                    "escalation_path": ["claims_handler_review"]}, "verification": out.get("verification")}
        # escalation: no coverage answer is returned; the human gets the findings
        tentative = route_claim(claim, handoff, None, self.s)
        M.ESCALATIONS  # counted where raised
        rec.emit("coverage", "escalation", "escalate", code=out["code"], detail=out["detail"], degraded=out.get("degraded"))
        path = ["claims_handler_review"] + (["engineering_oncall"] if out["code"] in ("PROVIDER_UNAVAILABLE", "AGENT_FAILURE", "LLM_OUTPUT_INVALID") else [])
        return {**base, "status": "escalated", "degraded": bool(out.get("degraded")), "recommendation": None,
                "escalation": {"to_human": True, "reason_code": out["code"], "detail": out["detail"], "escalation_path": path},
                "verification": out.get("verdict") or out.get("verification"),
                "partial_findings": {**out.get("partial", {}), "triage": {"claim_type": handoff["claim_type"], "entities": handoff["entities"],
                                     "risk_flags": handoff["risk_flags"], "inconsistencies": handoff["inconsistencies"]},
                                     "tentative_route": {**tentative, "note": "Tentative, from triage signals only - no coverage position was produced."}}}

    def _finish(self, run: dict, result: dict, out: dict, tools) -> None:
        rid = run["run_id"]
        self.store.update_run(rid, result=result, status=result["status"])
        e2e = result["timings"]["end_to_end_s"]
        M.E2E_LATENCY.labels(result["status"] + ("_degraded" if result["degraded"] else "")).observe(e2e)
        M.RUNS.labels("orchestrator", result["status"]).inc()
        # whole-run cache: only verified, non-degraded answers
        if result["status"] == "completed" and not result["degraded"] and self.s.run_cache_enabled and not result["security_flags"]:
            try:
                iv = tools._safe_ver() if tools else "?"
                fp = claim_fingerprint(run["claim"], run["meta"]["prompt_versions"], iv)
                self.store.cache_set("run", fp, {k: result[k] for k in ("recommendation", "escalation", "verification", "prompt_versions")}, ttl=self.s.cache_ttl_s)
            except Exception:
                pass
        if random.random() < self.s.quality_sample_rate and tools is not None and out["kind"] == "answer":
            quality.score_live_run(self.store, run, out, tools)
        log(logger, logging.INFO, "run finished", status=result["status"], e2e_s=e2e, cost_usd=result["cost_usd"],
            route=(result.get("recommendation") or {}).get("route"), code=(result.get("escalation") or {}).get("reason_code"))

    def _fail_run(self, run_id: str, why: str) -> None:
        run = self.store.get_run(run_id)
        if not run or run["status"] in TERMINAL:
            return
        handoff = run.get("handoff")
        res = {"run_id": run_id, "status": "escalated", "degraded": True, "recommendation": None,
               "escalation": {"to_human": True, "reason_code": "AGENT_FAILURE", "detail": why, "escalation_path": ["claims_handler_review", "engineering_oncall"]},
               "security_flags": run["meta"].get("security_flags", []), "prompt_versions": run["meta"].get("prompt_versions", {}),
               "partial_findings": {"triage": handoff} if handoff else None,
               "timings": {"end_to_end_s": round(time.time() - run["submitted_at"], 3)}}
        self.store.update_run(run_id, result=res, status="escalated")
        M.ESCALATIONS.labels("AGENT_FAILURE").inc()
        M.RUNS.labels("orchestrator", "escalated").inc()
        TraceRecorder(run_id, sink=lambda ev: self.store.append_event(run_id, ev)).emit(self.role, "escalation", "agent_failure", detail=why)


def main() -> None:
    s = get_settings()
    setup_logging(f"worker-{s.worker_role}", s.log_level)
    setup_tracing(f"worker-{s.worker_role}", s.otlp_endpoint)
    M.start_metrics_server(s.metrics_port, f"worker-{s.worker_role}")
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    w.run_forever()


if __name__ == "__main__":
    main()
