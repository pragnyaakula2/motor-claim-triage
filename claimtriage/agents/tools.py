"""Agent tools (A1). Retrieval is a tool the agent *chooses* to call, never a fixed stage.

Tools: search_policy, get_clause, list_cross_references, get_claim_fields.
All are read-only. Every call has a timeout, bounded retry with exponential backoff, argument
validation (a first line of defence against injected tool calls) and is recorded in the trace.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from typing import Any

import httpx

from .. import metrics as M
from ..config import Settings
from ..guardrails import sanitize_text
from ..logging_setup import CORRELATION_HEADER, correlation_id
from ..store import Store
from ..telemetry import span
from ..trace import TraceRecorder

log = logging.getLogger("tools")

CLAUSE_ID_RE = re.compile(r"^[A-Z0-9]{2,6}:[A-Z0-9\-]{1,24}:\d{2}$")
SECTION_TYPES = {"cover", "exclusion", "condition", "definition", "endorsement", "procedure", "admin"}
MODES = {"hybrid", "dense", "keyword"}
CLAIM_FIELDS = {
    "claim_type_declared", "date_of_loss", "date_reported", "policy_start_date", "policy_end_date",
    "estimated_repair_cost_inr", "third_party_involved", "injuries_reported", "claim_description", "claimed_damage",
    "vehicle.vehicle_class", "vehicle.drivable", "vehicle.idv_inr", "vehicle.year_of_manufacture",
    "driver.is_owner", "driver.licence_valid", "police_report.filed", "location.state",
}

TOOL_SPECS = {
    "search_policy": {"required": {"query"}, "optional": {"section_type", "top_k", "mode"}},
    "get_clause": {"required": {"clause_id"}, "optional": set()},
    "list_cross_references": {"required": {"clause_id"}, "optional": set()},
    "get_claim_fields": {"required": set(), "optional": {"fields"}},
}


class ToolError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


def validate_args(tool: str, args: Any) -> dict:
    if tool not in TOOL_SPECS:
        raise ToolError("unknown_tool", f"unknown tool {tool!r}; allowed: {sorted(TOOL_SPECS)}")
    if not isinstance(args, dict):
        raise ToolError("bad_args", "args must be an object")
    spec = TOOL_SPECS[tool]
    extra = set(args) - spec["required"] - spec["optional"]
    if extra:
        raise ToolError("bad_args", f"unexpected arguments: {sorted(extra)}")
    missing = spec["required"] - set(args)
    if missing:
        raise ToolError("bad_args", f"missing arguments: {sorted(missing)}")
    out = dict(args)
    if "query" in out:
        q = out["query"]
        if not isinstance(q, str) or not (2 <= len(q.strip()) <= 400):
            raise ToolError("bad_args", "query must be a string of 2-400 chars")
        out["query"] = q.strip()
    if "clause_id" in out and (not isinstance(out["clause_id"], str) or not CLAUSE_ID_RE.match(out["clause_id"])):
        raise ToolError("bad_args", "clause_id must look like PCPP:GE:01")
    if out.get("section_type") is not None and out["section_type"] not in SECTION_TYPES:
        raise ToolError("bad_args", f"section_type must be one of {sorted(SECTION_TYPES)}")
    if "top_k" in out:
        try:
            out["top_k"] = max(1, min(8, int(out["top_k"])))
        except (TypeError, ValueError):
            raise ToolError("bad_args", "top_k must be an integer 1-8")
    if out.get("mode") is not None and out["mode"] not in MODES:
        raise ToolError("bad_args", f"mode must be one of {sorted(MODES)}")
    if "fields" in out:
        if not isinstance(out["fields"], list) or not all(isinstance(f, str) and f in CLAIM_FIELDS for f in out["fields"]):
            raise ToolError("bad_args", f"fields must be a list drawn from {sorted(CLAIM_FIELDS)}")
    return out


def _dig(d: dict, dotted: str):
    cur: Any = d
    for p in dotted.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


class ToolBox:
    def __init__(self, settings: Settings, claim: dict, recorder: TraceRecorder, store: Store | None,
                 http: httpx.Client | None = None, agent: str = "coverage"):
        self.s, self.claim, self.rec, self.store, self.agent = settings, claim, recorder, store, agent
        self.http = http or httpx.Client(base_url=settings.retrieval_url, timeout=settings.tool_timeout_s)
        self.retrieved: dict[str, dict] = {}   # clause_id -> passage seen in THIS run (basis of citation checks)
        self.queries: list[dict] = []
        self.calls = 0
        self.modifier_searched = False
        self._index_version: str | None = None
        self.best_score = 0.0

    # -------------------------------------------------------------------------------------
    def _index_ver(self) -> str:
        if self._index_version is None:
            r = self.http.get("/health", headers={CORRELATION_HEADER: correlation_id.get()})
            self._index_version = r.json().get("index_version", "?")
        return self._index_version

    def _remote(self, method: str, path: str, json_body: dict | None = None) -> dict:
        last: Exception | None = None
        for attempt in range(self.s.tool_max_retries + 1):
            try:
                r = self.http.request(method, path, json=json_body, headers={CORRELATION_HEADER: correlation_id.get()})
                if r.status_code == 404:
                    raise ToolError("not_found", f"{path}: {r.json().get('detail', 'not found')}")
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError("5xx", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except ToolError:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                M.RETRIES.labels(self.agent, "tool").inc()
                if attempt < self.s.tool_max_retries:
                    time.sleep(0.2 * (2 ** attempt) * (0.5 + random.random()))
        raise ToolError("tool_unavailable", f"retrieval service unavailable: {type(last).__name__}")

    # -------------------------------------------------------------------------------------
    def call(self, tool: str, raw_args: Any) -> dict:
        """Execute a tool. Never raises: returns {'ok': bool, 'data'|'error': ...} and records the call."""
        t0 = time.time()
        self.calls += 1
        step = f"tool:{tool}"
        try:
            args = validate_args(tool, raw_args)
            with span(f"tool.{tool}", tool=tool, query=args.get("query")):
                data = getattr(self, f"_t_{tool}")(args)
            ok, err = True, None
        except ToolError as e:
            data, ok, err = None, False, {"code": e.code, "message": str(e)}
            args = raw_args if isinstance(raw_args, dict) else {"raw": str(raw_args)[:200]}
        dt = time.time() - t0
        M.TOOL_CALLS.labels(tool, "ok" if ok else (err or {}).get("code", "error")).inc()
        M.TOOL_LATENCY.labels(tool).observe(dt)
        M.STEP_LATENCY.labels(self.agent, step).observe(dt)
        M.STEPS_TOTAL.labels(self.agent, step).inc()
        summary = None
        if ok and isinstance(data, dict) and "passages" in data:
            summary = [{"clause_id": p["clause_id"], "score": p["score"], "page": p["page_start"],
                        "section": p["section"]} for p in data["passages"]]
        self.rec.emit(self.agent, "tool_call", step, tool=tool, args=args, ok=ok, error=err,
                      latency_s=round(dt, 4), cache=getattr(self, "_last_cache", None),
                      passages=summary, result_preview=None if summary else (json.dumps(data)[:300] if ok else None))
        return {"ok": ok, "data": data, "error": err}

    # ---- tools -------------------------------------------------------------------------------------
    def _t_search_policy(self, a: dict) -> dict:
        body = {"query": a["query"], "top_k": a.get("top_k", 5), "section_type": a.get("section_type"), "mode": a.get("mode", "hybrid")}
        if body["section_type"] in ("exclusion", "condition") or body["section_type"] is None:
            pass
        key = hashlib.sha256(json.dumps([self._safe_ver(), body], sort_keys=True).encode()).hexdigest()[:24]
        self._last_cache = "miss"
        data = self.store.cache_get("retrieval", key) if self.store else None
        if data is None:
            data = self._remote("POST", "/search", body)
            if self.store:
                self.store.cache_set("retrieval", key, data)
        else:
            self._last_cache = "hit"
        if body["section_type"] in ("exclusion", "condition"):
            self.modifier_searched = True
        self.queries.append({"query": body["query"], "section_type": body["section_type"], "mode": body["mode"],
                             "top_score": data["top_score"], "n": len(data["passages"])})
        for p in data["passages"]:
            self.retrieved.setdefault(p["clause_id"], p)
            self.best_score = max(self.best_score, p["score"])
        return data

    def _safe_ver(self) -> str:
        try:
            return self._index_ver()
        except Exception:
            return "?"

    def _t_get_clause(self, a: dict) -> dict:
        self._last_cache = None
        p = self._remote("GET", f"/clause/{a['clause_id']}")
        self.retrieved.setdefault(p["clause_id"], p)
        return {"passages": [p], "index_version": self._safe_ver(), "top_score": 1.0, "mode": "lookup"}

    def _t_list_cross_references(self, a: dict) -> dict:
        self._last_cache = None
        d = self._remote("GET", f"/crossrefs/{a['clause_id']}")
        self.modifier_searched = True
        return d

    def _t_get_claim_fields(self, a: dict) -> dict:
        self._last_cache = None
        fields = a.get("fields") or sorted(CLAIM_FIELDS)
        out = {}
        for f in fields:
            v = _dig(self.claim, f)
            out[f] = sanitize_text(v) if isinstance(v, str) else v
        return {"fields": out}


class InProcessToolBox(ToolBox):
    """Same tools, but the 'retrieval service' is a PolicyIndex in this process (used by the offline evaluation harness and tests)."""

    def __init__(self, *a, index=None, **kw):
        kw.setdefault("http", httpx.Client(base_url="http://unused"))
        super().__init__(*a, **kw)
        self.ix = index

    def _remote(self, method: str, path: str, json_body: dict | None = None) -> dict:
        if path == "/search":
            b = json_body
            res = self.ix.search(b["query"], b["top_k"], b.get("section_type"), b.get("mode", "hybrid"))
            return {"passages": res, "index_version": self.ix.version, "mode": b.get("mode", "hybrid"), "top_score": max((r["score"] for r in res), default=0.0)}
        if path.startswith("/clause/"):
            c = self.ix.get_clause(path.split("/clause/", 1)[1])
            if not c:
                raise ToolError("not_found", "unknown clause_id")
            return c
        if path.startswith("/crossrefs/"):
            r = self.ix.cross_refs(path.split("/crossrefs/", 1)[1])
            if r is None:
                raise ToolError("not_found", "unknown clause_id")
            return r
        raise ToolError("tool_unavailable", path)

    def _safe_ver(self) -> str:
        return self.ix.version
