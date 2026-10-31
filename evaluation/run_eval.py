"""Batch evaluation (L3):  python evaluation/run_eval.py --label before --prompt-coverage v0

Runs every scenario in evaluation/scenarios.jsonl and reports outcome accuracy plus groundedness, context relevance,
faithfulness and hallucination rate (definitions: claimtriage/evalmetrics.py, docs/EVALUATION.md).

  --mode inproc  agents + local index in this process (no Redis/queue); LLM endpoint taken from LLM_* env (stub or a real provider)
  --mode http    through the deployed API (needs `docker compose up` / scripts/devstack.sh)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claimtriage.agents.common import norm_text  # noqa: E402
from claimtriage.evalmetrics import claim_facts_text, score_answer, score_retrieval  # noqa: E402
from claimtriage.samples import base_fnol  # noqa: E402


def load_scenarios(only: list[str] | None) -> list[dict]:
    rows = [json.loads(l) for l in (ROOT / "evaluation" / "scenarios.jsonl").read_text().splitlines() if l.strip()]
    return [r for r in rows if not only or r["id"] in only]


def payload_for(sc: dict) -> dict:
    return base_fnol(**sc["overrides"])


# ----------------------------------------------------------------------------- execution backends
class InProc:
    def __init__(self):
        from claimtriage.config import get_settings
        from claimtriage.llm.client import LLMClient
        from claimtriage.retrieval.embedder import get_embedder
        from claimtriage.retrieval.index import PolicyIndex
        self.s = get_settings()
        self.llm = LLMClient(self.s)
        self.ix = PolicyIndex(self.s.data_dir / "index", get_embedder(self.s.embedding_backend, self.s.embedding_model))

    def run(self, sc: dict) -> dict:
        from claimtriage.agents.coverage import CoverageAgent
        from claimtriage.agents.router import route_claim
        from claimtriage.agents.tools import InProcessToolBox
        from claimtriage.agents.triage import TriageAgent
        from claimtriage.guardrails import scan_inputs
        from claimtriage.trace import TraceRecorder
        from claimtriage.validation import validate_fnol
        t0 = time.time()
        model, errs = validate_fnol(payload_for(sc))
        if errs:
            return {"outcome": "rejected", "reason": "validation", "errors": [e.field for e in errs], "retrieved": {}, "events": [], "latency_s": time.time() - t0}
        claim = model.model_dump(mode="json")
        g = scan_inputs({k: claim[k] for k in ("claim_description", "claimed_damage")}, self.s.injection_block_score, self.s.injection_flag_score)
        if g["action"] == "block":
            return {"outcome": "rejected", "reason": "guardrail", "errors": g["flags"], "retrieved": {}, "events": [], "latency_s": time.time() - t0}
        rec = TraceRecorder("eval")
        ho = TriageAgent(self.llm, self.s, rec).run(claim, g["sanitized"], g["flags"])
        tb = InProcessToolBox(self.s, claim, rec, None, index=self.ix)
        out = CoverageAgent(self.llm, self.s, rec, tb, claim, g["sanitized"], ho).run()
        res = {"events": rec.events, "retrieved": tb.retrieved, "latency_s": time.time() - t0, "handoff": ho,
               "tokens": rec.totals(), "claim_text": f'{claim["claim_description"]} {claim["claimed_damage"]} {claim_facts_text(claim)}'}
        if out["kind"] == "answer":
            a = out["answer"]
            r = route_claim(claim, ho, a["position"], self.s)
            return {**res, "outcome": "answer", "position": a["position"], "route": r["route"], "use": a.get("within_permitted_use"), "answer": a}
        return {**res, "outcome": "escalation", "code": out["code"], "detail": out["detail"]}


class Http:
    def __init__(self, base: str, retrieval: str):
        import httpx
        self.h = httpx.Client(base_url=base, timeout=30)
        self.r = httpx.Client(base_url=retrieval, timeout=10)
        self.cache: dict = {}

    def run(self, sc: dict) -> dict:
        t0 = time.time()
        while True:
            r = self.h.post("/v1/claims", json=payload_for(sc), headers={"X-Bypass-Cache": "1", "X-Client-Id": "eval-batch"})
            if r.status_code != 429:
                break
            time.sleep(float(r.headers.get("Retry-After", 2)))
        if r.status_code == 422:
            return {"outcome": "rejected", "reason": r.json().get("errors", [{}])[0].get("code"), "errors": [], "retrieved": {}, "events": [], "latency_s": time.time() - t0}
        run_id = r.json()["run_id"]
        while True:
            s = self.h.get(f"/v1/runs/{run_id}").json()
            if s["status"] in ("completed", "escalated", "failed"):
                break
            time.sleep(0.25)
        tr = self.h.get(f"/v1/runs/{run_id}/trace").json()
        retrieved = {}
        for e in tr["events"]:
            if e["type"] == "tool_call" and e.get("passages"):
                for p in e["passages"]:
                    if p["clause_id"] not in retrieved:
                        c = self.cache.get(p["clause_id"]) or self.r.get(f"/clause/{p['clause_id']}").json()
                        self.cache[p["clause_id"]] = c
                        retrieved[p["clause_id"]] = c
        res = s["result"]
        base = {"events": tr["events"], "retrieved": retrieved, "latency_s": time.time() - t0, "tokens": tr["totals"],
                "handoff": next((e["handoff"] for e in tr["events"] if e["type"] == "handoff"), {}), "run_id": run_id}
        rec = res.get("recommendation")
        if rec:
            pl = payload_for(sc)
            base["claim_text"] = pl["claim_description"] + " " + pl["claimed_damage"] + " " + claim_facts_text(pl)
            return {**base, "outcome": "answer", "position": rec["coverage_position"], "route": rec["route"], "use": rec["within_permitted_use"],
                    "answer": {"position": rec["coverage_position"], "summary": rec["summary"], "citations": rec["citations"],
                               "within_permitted_use": rec["within_permitted_use"],
                               "restriction_assessment": next((e["draft"].get("restriction_assessment") for e in tr["events"][::-1] if e["type"] == "verification" and e.get("passed")), [])}}
        return {**base, "outcome": "escalation", "code": res["escalation"]["reason_code"], "detail": res["escalation"]["detail"]}


# ----------------------------------------------------------------------------- scoring
def score(sc: dict, r: dict) -> dict:
    exp = sc["expected"]
    out = {"id": sc["id"], "title": sc["title"], "expected": exp, "outcome": r["outcome"], "latency_s": round(r["latency_s"], 2)}
    ok = {"outcome": r["outcome"] == exp["outcome"]}
    if exp["outcome"] == "answer" and r["outcome"] == "answer":
        ok["position"] = r["position"] == exp["position"]
        ok["route"] = r["route"] == exp["route"]
        if exp.get("within_permitted_use"):
            ok["use"] = r["use"] == exp["within_permitted_use"]
    if exp["outcome"] == "escalation" and r["outcome"] == "escalation":
        ok["code"] = r.get("code") == exp["escalation_code"]
    out.update(position=r.get("position"), route=r.get("route"), use=r.get("use"), code=r.get("code"), checks=ok)
    out["success"] = all(ok.values()) if exp["outcome"] != "answer" or r["outcome"] == "answer" else False

    ret = score_retrieval(r["retrieved"], sc["gold_anchors"]) if r["outcome"] != "rejected" else {"context_relevance": None, "context_recall": None}
    out.update(ret)
    # first draft (before the verifier acted)
    ver = [e for e in r["events"] if e["type"] == "verification"]
    out["verification_attempts"] = len(ver)
    out["first_draft_rejected"] = bool(ver and not ver[0]["passed"])
    if r["outcome"] == "answer":
        a = r["answer"]
        cit_hits = []
        for anc in sc["expected_citations"]:
            na = norm_text(anc)
            cit_hits.append(any(na in norm_text(r["retrieved"].get(c["clause_id"], {}).get("text", "")) for c in a.get("citations", [])))
        out["expected_citation_hit"] = round(sum(cit_hits) / len(cit_hits), 3) if cit_hits else None
        sm = score_answer(a, r["retrieved"], r.get("claim_text", ""), r.get("handoff", {}))
        out.update(groundedness=sm["groundedness"], faithfulness=sm["faithfulness"], hallucination=sm["hallucination"], ungrounded=sm["ungrounded_sentences"], unresolved=sm["unresolved_citations"], unverbatim=sm["unverbatim_quotes"], bad_figures=sm["unsupported_figures"])
    if ver and ver[0].get("draft") and ver[0]["draft"].get("position") not in (None, "INSUFFICIENT_EVIDENCE", "OUT_OF_CORPUS"):
        d = score_answer(ver[0]["draft"], r["retrieved"], r.get("claim_text", ""), r.get("handoff", {}))
        out.update(draft_groundedness=d["groundedness"], draft_faithfulness=d["faithfulness"], draft_hallucination=d["hallucination"])
    tk = r.get("tokens") or {}
    out["tokens"] = (tk.get("prompt_tokens", 0), tk.get("completion_tokens", 0))
    out["llm_calls"] = tk.get("llm_calls", 0)
    out["tool_calls"] = tk.get("tool_calls", 0)
    return out


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def aggregate(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["outcome"] == "answer"]
    exp_ans = [r for r in rows if r["expected"]["outcome"] == "answer"]
    exp_esc = [r for r in rows if r["expected"]["outcome"] == "escalation"]
    exp_rej = [r for r in rows if r["expected"]["outcome"] == "rejected"]
    return {
        "scenarios": len(rows),
        "task_success_rate": mean([int(r["success"]) for r in rows]),
        "outcome_accuracy": mean([int(r["checks"]["outcome"]) for r in rows]),
        "answer_rate_on_answerable": mean([int(r["outcome"] == "answer") for r in exp_ans]),
        "position_accuracy": mean([int(r["checks"].get("position", False)) for r in exp_ans]),
        "route_accuracy": mean([int(r["checks"].get("route", False)) for r in exp_ans]),
        "escalation_recall_on_unanswerable": mean([int(r["outcome"] == "escalation") for r in exp_esc]),
        "fabricated_answers_on_unanswerable": mean([int(r["outcome"] == "answer") for r in exp_esc]),
        "rejection_recall": mean([int(r["outcome"] == "rejected") for r in exp_rej]),
        "section_4_1_correct": next((int(r["success"]) for r in rows if r["id"] == "E02"), None),
        "expected_citation_hit_rate": mean([r.get("expected_citation_hit") for r in ans]),
        # the four required metrics (returned answers)
        "context_relevance": mean([r.get("context_relevance") for r in rows]),
        "context_recall": mean([r.get("context_recall") for r in rows]),
        "groundedness": mean([r.get("groundedness") for r in ans]),
        "faithfulness": mean([r.get("faithfulness") for r in ans]),
        "hallucination_rate_returned": mean([int(r["hallucination"]) for r in ans]),
        # before the verifier acted
        "draft_groundedness": mean([r.get("draft_groundedness") for r in rows]),
        "draft_faithfulness": mean([r.get("draft_faithfulness") for r in rows]),
        "draft_hallucination_rate": mean([int(r["draft_hallucination"]) for r in rows if "draft_hallucination" in r]),
        "first_draft_rejected_by_verifier": mean([int(r["first_draft_rejected"]) for r in rows if r["verification_attempts"]]),
        "mean_llm_calls": mean([r["llm_calls"] for r in rows if r["outcome"] != "rejected"]),
        "mean_tool_calls": mean([r["tool_calls"] for r in rows if r["outcome"] != "rejected"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--mode", default="inproc", choices=["inproc", "http"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--prompt-coverage")
    ap.add_argument("--prompt-triage")
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--retrieval", default=os.environ.get("RETRIEVAL_URL", "http://localhost:8100"))
    ap.add_argument("--out", default=str(ROOT / "evaluation" / "results"))
    a = ap.parse_args()
    if a.prompt_coverage:
        os.environ["PROMPT_VERSION_COVERAGE"] = a.prompt_coverage
    if a.prompt_triage:
        os.environ["PROMPT_VERSION_TRIAGE"] = a.prompt_triage
    from claimtriage.config import get_settings, reset_settings_cache
    reset_settings_cache()
    runner = InProc() if a.mode == "inproc" else Http(a.api, a.retrieval)
    rows = []
    for sc in load_scenarios(a.only):
        r = runner.run(sc)
        row = score(sc, r)
        rows.append(row)
        mark = "OK " if row["success"] else "XX "
        print(f'{mark}{sc["id"]} {sc["title"][:48]:48} -> {row["outcome"]:10} {str(row.get("position") or row.get("code") or ""):34} {str(row.get("route") or ""):20} {row["latency_s"]}s')
    agg = aggregate(rows)
    s = get_settings()
    meta = {"label": a.label, "mode": a.mode, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "prompt_coverage": s.prompt_coverage,
            "prompt_triage": s.prompt_triage, "embedding_backend": s.embedding_backend, "llm_model_configured": bool(s.llm_model),
            "llm_base_url_host": s.llm_base_url.split("/")[2] if s.llm_base_url else None}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / f"{a.label}.json").write_text(json.dumps({"meta": meta, "aggregate": agg, "scenarios": rows}, indent=1, default=str))
    print("\n" + json.dumps(agg, indent=1))


if __name__ == "__main__":
    main()
