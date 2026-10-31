"""Coverage Agent (agent 2 of 2): a LangGraph loop that decides which tool to call (A1, F4, F5, F7).

            +---------+     tool      +------+
 init ----> | decide  | ------------> | act  | --+
            +---------+ <-------------+------+   |
              | final      (loop, bounded)       |
              v                                  |
            +--------+  fail & attempts left  +--------+
            | verify | ---------------------> | replan | --> decide (different strategy)
            +--------+                        +--------+
              | pass            | fail, no attempts left / weak retrieval / conflict / budget
              v                 v
          finalize           escalate        degrade (LLM unavailable -> retrieval-only evidence pack -> escalate)

Bounds: max steps per attempt, max tool calls, wall-clock deadline, max verification attempts.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .. import metrics as M
from ..config import Settings
from ..guardrails import filter_output
from ..llm.client import LLMBadResponse, LLMClient, ProviderUnavailable
from ..llm.prompts import load_prompt
from ..telemetry import span
from ..trace import TraceRecorder
from .common import parse_json_object
from .tools import ToolBox
from .verifier import verify_answer

log = logging.getLogger("coverage")

STRATEGIES = {
    1: "semantic_chase: retrieve the grant of cover, read its cross-references, then search again for each modifying provision with a query derived from what you read and the claim's use of the vehicle.",
    2: "keyword_sections: your previous answer failed verification. Search with mode=keyword and section_type filters (exclusion, condition), look up every clause you intend to cite with get_clause, and copy quotes verbatim from the returned text.",
    3: "minimal_evidence: cite only passages returned by get_clause in this attempt, keep the summary short and free of figures, or escalate if the evidence is not there.",
}


class CState(TypedDict, total=False):
    messages: list[dict]
    steps: int
    attempt: int
    strategy: int
    forced: bool
    invalid_streak: int
    draft: dict | None
    verdict: dict | None
    outcome: dict | None
    degraded: bool
    last_codes: list[str]
    route_next: str
    pending: dict


def _tool_result_message(name: str, res: dict) -> str:
    if not res["ok"]:
        return f"TOOL_ERROR name={name} code={res['error']['code']} message={res['error']['message']}"
    data = res["data"]
    if "passages" in data:
        slim = {"passages": [{k: p[k] for k in ("clause_id", "section", "section_type", "page_start", "page_end", "score", "text")}
                             for p in data["passages"]]}
    else:
        slim = data
    return f"TOOL_RESULT name={name}\n" + json.dumps(slim, ensure_ascii=True)


class CoverageAgent:
    name = "coverage"

    def __init__(self, llm: LLMClient, settings: Settings, recorder: TraceRecorder, tools: ToolBox, claim: dict,
                 sanitized: dict, handoff: dict, started_at: float | None = None):
        self.llm, self.s, self.rec, self.tools = llm, settings, recorder, tools
        self.claim, self.sanitized, self.handoff = claim, sanitized, handoff
        self.t0 = started_at or time.time()
        self.deadline = time.time() + settings.agent_deadline_s
        self.prompt = load_prompt("coverage", settings.prompt_coverage)
        self.graph = self._build()

    # ------------------------------------------------------------------ graph
    def _build(self):
        g = StateGraph(CState)
        for n in ("init", "decide", "act", "verify", "replan", "finalize", "escalate", "degrade"):
            g.add_node(n, getattr(self, f"n_{n}"))
        g.set_entry_point("init")
        g.add_conditional_edges("init", lambda s: s["route_next"], {"decide": "decide", "escalate": "escalate"})
        g.add_conditional_edges("decide", lambda s: s["route_next"],
                                {"act": "act", "verify": "verify", "escalate": "escalate", "degrade": "degrade", "decide": "decide"})
        g.add_edge("act", "decide")
        g.add_conditional_edges("verify", lambda s: s["route_next"], {"finalize": "finalize", "replan": "replan", "escalate": "escalate"})
        g.add_edge("replan", "decide")
        g.add_conditional_edges("finalize", lambda s: s["route_next"], {"end": END, "escalate": "escalate"})
        g.add_edge("escalate", END)
        g.add_edge("degrade", "escalate")
        return g.compile()

    def run(self) -> dict:
        t0 = time.time()
        with span("agent.coverage"):
            final = self.graph.invoke({"steps": 0, "attempt": 1, "strategy": 1, "forced": False, "invalid_streak": 0,
                                       "degraded": False, "last_codes": []}, {"recursion_limit": 120})
        out = final["outcome"]
        dt = time.time() - t0
        M.AGENT_RUN_LATENCY.labels(self.name).observe(dt)
        M.RUN_TOOLCALLS.labels(self.name).observe(self.tools.calls)
        M.RUN_STEPS.labels(self.name).observe(final.get("steps", 0))
        kind = "degraded" if out.get("degraded") else ("completed" if out["kind"] == "answer" else "escalated")
        M.RUNS.labels(self.name, kind).inc()
        out["steps"] = final.get("steps", 0)
        out["tool_calls"] = self.tools.calls
        out["attempts"] = final.get("attempt", 1)
        return out

    # ------------------------------------------------------------------ helpers
    def _user_prompt(self, strategy: int, feedback: str | None = None) -> str:
        view = {
            "claim_type": self.handoff["claim_type"], "claim_type_declared": self.claim["claim_type_declared"],
            "claim_description": self.sanitized["claim_description"], "claimed_damage": self.sanitized["claimed_damage"],
            "vehicle_class": self.claim["vehicle"]["vehicle_class"], "driver_is_owner": self.claim["driver"]["is_owner"],
            "police_report_filed": self.claim["police_report"]["filed"], "third_party_involved": self.claim["third_party_involved"],
        }
        ho = {"claim_type": self.handoff["claim_type"], "entities": self.handoff["entities"],
              "risk_flags": self.handoff["risk_flags"], "inconsistencies": self.handoff["inconsistencies"]}
        txt = ("<claim_json>" + json.dumps(view, ensure_ascii=True) + "</claim_json>\n"
               "<handoff_json>" + json.dumps(ho, ensure_ascii=True) + "</handoff_json>\n"
               f"STRATEGY {strategy}: {STRATEGIES[strategy]}\n")
        if feedback:
            txt += f"VERIFICATION_FAILED: {feedback}\nPreviously retrieved clause_ids: {sorted(self.tools.retrieved)}\n"
        return txt + "Begin."

    def _over_budget(self, s: CState) -> str | None:
        if time.time() >= self.deadline:
            return "wall-clock deadline"
        if self.tools.calls >= self.s.agent_max_tool_calls:
            return "tool-call budget"
        if s["steps"] >= self.s.agent_max_steps * self.s.agent_max_attempts:
            return "total step budget"
        return None

    # ------------------------------------------------------------------ nodes
    def n_init(self, s: CState) -> CState:
        if self.claim["vehicle"]["vehicle_class"] != "private_car":
            self.rec.emit(self.name, "guard", "scope_check", result="out_of_corpus",
                          detail=f"vehicle_class={self.claim['vehicle']['vehicle_class']}; corpus covers the private car package policy only")
            return {"route_next": "escalate", "outcome": self._esc("OUT_OF_CORPUS",
                    f"Vehicle class '{self.claim['vehicle']['vehicle_class']}' is outside the indexed corpus (private car package policy).")}
        scope = self.handoff.get("scope") or {"in_scope": True}
        if not scope["in_scope"]:
            why = ("the narrative does not describe a motor-vehicle loss" if scope.get("motor_terms", 1) == 0
                   else f"it concerns topics the indexed wording does not address ({', '.join(scope['out_of_wording_topics'])})")
            self.rec.emit(self.name, "guard", "scope_check", result="out_of_corpus", detail=why)
            return {"route_next": "escalate", "outcome": self._esc("OUT_OF_CORPUS", f"Claim is outside the corpus: {why}.")}
        msgs = [{"role": "system", "content": self.prompt.text}, {"role": "user", "content": self._user_prompt(1)}]
        return {"messages": msgs, "route_next": "decide"}

    def n_decide(self, s: CState) -> CState:
        t0 = time.time()
        steps = s["steps"]
        over = self._over_budget(s)
        attempt_steps = steps - (s["attempt"] - 1) * self.s.agent_max_steps
        msgs = list(s["messages"])
        forced = s.get("forced", False)
        if (over or attempt_steps >= self.s.agent_max_steps) and not forced:
            msgs.append({"role": "user", "content": f"BUDGET_REACHED ({over or 'step budget'}). Do not call tools. Give your final answer from the evidence already retrieved, or escalate."})
            forced = True
        elif forced:
            return {"route_next": "escalate", "outcome": self._esc("BUDGET_EXHAUSTED", "Agent did not produce a final answer within its step/tool/time budget.")}
        step_name = f"decide#{steps + 1}"
        with span("agent.step", step=step_name):
            try:
                res = self.llm.chat(msgs, agent=self.name, step="decide", max_tokens=900, deadline=self.deadline + 10)
            except (ProviderUnavailable, LLMBadResponse) as e:
                self.rec.emit(self.name, "llm_call", step_name, ok=False, error=str(e)[:200], prompt_version=self.prompt.id,
                              latency_s=round(time.time() - t0, 3), breaker=self.llm.breaker.name)
                return {"route_next": "degrade", "degraded": True, "steps": steps + 1}
        dt = time.time() - t0
        M.STEP_LATENCY.labels(self.name, "decide").observe(dt)
        M.STEPS_TOTAL.labels(self.name, "decide").inc()
        ev = dict(ok=True, prompt_version=self.prompt.id, latency_s=round(res.latency_s, 3), prompt_tokens=res.prompt_tokens,
                  completion_tokens=res.completion_tokens, cost_usd=round(res.cost_usd, 6), model=res.model, attempts=res.attempts,
                  attempt=s["attempt"], strategy=s["strategy"])
        try:
            act = parse_json_object(res.text)
            kind = act.get("action")
            if kind not in ("tool", "final"):
                raise ValueError("'action' must be 'tool' or 'final'")
            if kind == "tool" and ("tool" not in act or not isinstance(act.get("args", {}), dict)):
                raise ValueError("tool action needs 'tool' and an 'args' object")
            if kind == "final" and not isinstance(act.get("answer"), dict):
                raise ValueError("final action needs an 'answer' object")
        except ValueError as e:
            self.rec.emit(self.name, "llm_call", step_name, parse_error=str(e)[:160], raw=res.text[:300], **{**ev, "ok": False})
            M.RETRIES.labels(self.name, "parse").inc()
            streak = s.get("invalid_streak", 0) + 1
            if streak >= 3:
                return {"route_next": "escalate", "steps": steps + 1,
                        "outcome": self._esc("LLM_OUTPUT_INVALID", f"Model produced invalid output {streak} times in a row: {e}")}
            msgs += [{"role": "assistant", "content": res.text[:600]},
                     {"role": "user", "content": f"INVALID_OUTPUT: {e}. Reply with ONE JSON object: a tool call or a final answer."}]
            return {"messages": msgs, "steps": steps + 1, "invalid_streak": streak, "route_next": "decide", "forced": forced}
        self.rec.emit(self.name, "llm_call", step_name, action=kind, tool=act.get("tool"), thought=str(act.get("thought", ""))[:240], **ev)
        msgs.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=True)})
        if kind == "tool":
            if forced:
                return {"route_next": "escalate", "steps": steps + 1,
                        "outcome": self._esc("BUDGET_EXHAUSTED", "Agent asked for another tool after its budget was reached.")}
            return {"messages": msgs, "steps": steps + 1, "invalid_streak": 0, "pending": act, "route_next": "act", "forced": forced}
        return {"messages": msgs, "steps": steps + 1, "invalid_streak": 0, "draft": act["answer"], "route_next": "verify", "forced": forced}

    def n_act(self, s: CState) -> CState:
        act = s["pending"]  # type: ignore[typeddict-item]
        res = self.tools.call(act["tool"], act.get("args", {}))
        msgs = list(s["messages"]) + [{"role": "user", "content": _tool_result_message(act["tool"], res)}]
        return {"messages": msgs}

    def n_verify(self, s: CState) -> CState:
        t0 = time.time()
        draft = s["draft"] or {}
        with span("agent.verify"):
            verdict = verify_answer(draft, self.tools.retrieved, handoff=self.handoff, claim=self.claim,
                                    modifier_searched=self.tools.modifier_searched, best_score=self.tools.best_score,
                                    weak_threshold=self.s.weak_retrieval_threshold)
            if verdict.passed and self.s.verifier_llm_check and draft.get("citations") and not draft.get("needs_escalation"):
                self._llm_entailment(draft, verdict)
        dt = time.time() - t0
        M.STEP_LATENCY.labels(self.name, "verify").observe(dt)
        M.STEPS_TOTAL.labels(self.name, "verify").inc()
        M.VERIFY.labels("passed" if verdict.passed else "failed").inc()
        self.rec.emit(self.name, "verification", f"verify#{s['attempt']}", attempt=s["attempt"], strategy=s["strategy"],
                      passed=verdict.passed, verdict=verdict.to_dict(), position=draft.get("position"),
                      draft={k: draft.get(k) for k in ("position", "summary", "within_permitted_use", "restriction_assessment", "citations")},
                      citations=[c.get("clause_id") for c in draft.get("citations", []) if isinstance(c, dict)],
                      latency_s=round(dt, 4))
        if verdict.passed:
            return {"verdict": verdict.to_dict(), "route_next": "finalize"}
        if s["attempt"] >= self.s.agent_max_attempts or self._over_budget(s):
            code = "WEAK_RETRIEVAL" if "WEAK_RETRIEVAL" in verdict.codes else "VERIFICATION_FAILED"
            return {"verdict": verdict.to_dict(), "route_next": "escalate", "last_codes": verdict.codes,
                    "outcome": self._esc(code, "Answer could not be verified against retrieved passages: " + "; ".join(verdict.issues[:4]),
                                         verdict=verdict.to_dict())}
        return {"verdict": verdict.to_dict(), "route_next": "replan", "last_codes": verdict.codes}

    def _llm_entailment(self, draft: dict, verdict) -> None:
        judge = load_prompt("judge", "v1")
        for c in draft.get("citations", [])[:3]:
            p = self.tools.retrieved.get(c.get("clause_id"))
            if not p:
                continue
            stmt = (draft.get("summary") or "")[:500]
            try:
                r = self.llm.chat([{"role": "system", "content": judge.text},
                                   {"role": "user", "content": json.dumps({"statement": stmt, "passage": p["text"][:1200]})}],
                                  agent=self.name, step="verify_llm", max_tokens=120, deadline=self.deadline + 10)
                self.rec.emit(self.name, "llm_call", "verify_llm", ok=True, prompt_version=judge.id,
                              prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens, cost_usd=round(r.cost_usd, 6),
                              latency_s=round(r.latency_s, 3))
                if parse_json_object(r.text).get("supported") is False:
                    verdict.passed = False
                    verdict.issues.append(f"V8_llm_entailment: summary not supported by {c['clause_id']}")
                    verdict.codes.append("UNSUPPORTED_CLAIM")
                    return
            except (ProviderUnavailable, LLMBadResponse, ValueError):
                return  # the judge is optional; deterministic checks already passed

    def n_replan(self, s: CState) -> CState:
        attempt = s["attempt"] + 1
        strategy = min(attempt, 3)
        M.RETRIES.labels(self.name, "verification").inc()
        feedback = "; ".join((s["verdict"] or {}).get("issues", []))[:600]
        msgs = [{"role": "system", "content": self.prompt.text}, {"role": "user", "content": self._user_prompt(strategy, feedback)}]
        self.rec.emit(self.name, "replan", f"replan#{attempt}", attempt=attempt, strategy=strategy, reason=feedback)
        return {"messages": msgs, "attempt": attempt, "strategy": strategy, "forced": False, "draft": None}

    def n_finalize(self, s: CState) -> CState:
        draft = s["draft"] or {}
        allowed = set()
        for p in self.tools.retrieved.values():
            pass
        clean, violations = filter_output(draft, allowed_amounts=self._allowed_amounts())
        self.rec.emit(self.name, "output_filter", "finalize", violations=violations)
        if any(v.startswith("position_not_allowed") for v in violations):
            return {"route_next": "escalate", "outcome": self._esc("OUTPUT_FILTER", "Model produced a position outside the allowed set.")}
        if draft.get("needs_escalation") or draft.get("position") in ("INSUFFICIENT_EVIDENCE", "OUT_OF_CORPUS") or draft.get("conflicting_clauses"):
            if draft.get("conflicting_clauses"):
                code = "CLAUSE_CONFLICT"
            elif draft.get("position") == "OUT_OF_CORPUS":
                code = "OUT_OF_CORPUS"
            elif self.tools.best_score < self.s.weak_retrieval_threshold:
                code = "WEAK_RETRIEVAL"
            else:
                code = "INSUFFICIENT_EVIDENCE"
            return {"route_next": "escalate", "outcome": self._esc(code, clean.get("escalation_reason") or clean.get("summary", "")[:300],
                                                                   verdict=s.get("verdict"), draft=clean)}
        cits = []
        for c in clean.get("citations", []):
            p = self.tools.retrieved[c["clause_id"]]
            cits.append({"clause_id": c["clause_id"], "source": p["source"], "page_start": p["page_start"], "page_end": p["page_end"],
                         "section": p["section"], "role": c.get("role", "cover"), "quote": c["quote"]})
        outcome = {"kind": "answer", "answer": {**clean, "citations": cits}, "verification": s.get("verdict"),
                   "output_violations": violations, "degraded": False}
        return {"route_next": "end", "outcome": outcome}

    def _allowed_amounts(self) -> set[float]:
        vals = set()
        for k in ("estimated_repair_cost_inr",):
            if self.claim.get(k):
                vals.add(float(self.claim[k]))
        vals.add(float(self.claim["vehicle"]["idv_inr"]))
        vals.add(float(self.s.fast_track_max_inr))
        import re
        for p in self.tools.retrieved.values():
            for m in re.finditer(r"(?:rs\.?|inr|₹)\s?([\d,]+(?:\.\d+)?)", p["text"], re.I):
                try:
                    vals.add(float(m.group(1).replace(",", "")))
                except ValueError:
                    pass
        return vals

    def n_degrade(self, s: CState) -> CState:
        """Fallback path: provider unavailable -> retrieval-only evidence pack for the human, no generated answer."""
        M.DEGRADED.labels("retrieval_only").inc()
        ctype = self.handoff["claim_type"]
        queries = {
            "own_damage": ("loss or damage to the vehicle insured accidental external means", None),
            "theft": ("burglary housebreaking or theft loss of vehicle", None),
            "third_party": ("liability to third parties death bodily injury property damage", None),
            "total_loss": ("total loss constructive total loss insured declared value", None),
        }
        plan = [queries[ctype], ("general exceptions use of the vehicle limitations as to use hire or reward", "exclusion"),
                ("conditions notice claim duties of the insured", "condition")]
        for q, st in plan:
            if self.tools.calls >= self.s.agent_max_tool_calls + 3:
                break
            args = {"query": q, "top_k": 3}
            if st:
                args["section_type"] = st
            self.tools.call("search_policy", args)
        return {"degraded": True}

    def n_escalate(self, s: CState) -> CState:
        out = s.get("outcome")
        if out is None:  # arrived via degrade
            out = self._esc("PROVIDER_UNAVAILABLE", "LLM provider unavailable (circuit breaker open or retries exhausted); "
                            "retrieval-only evidence pack attached for the human handler.", degraded=True)
        out["degraded"] = bool(s.get("degraded") or out.get("degraded"))
        return {"outcome": out}

    def _esc(self, code: str, detail: str, **extra) -> dict:
        M.ESCALATIONS.labels(code).inc()
        partial = {
            "evidence": [{"clause_id": cid, "section": p["section"], "section_type": p["section_type"], "page": p["page_start"],
                          "score": p["score"], "excerpt": p["text"][:240]} for cid, p in list(self.tools.retrieved.items())[:10]],
            "queries": self.tools.queries[-10:],
        }
        return {"kind": "escalation", "code": code, "detail": detail[:600], "partial": partial, "degraded": extra.pop("degraded", False),
                **extra}
