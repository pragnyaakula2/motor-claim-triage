"""Agent behaviour end-to-end without network: scripted policy as the LLM, local index as the retrieval service."""
import json

import pytest

from claimtriage.agents.coverage import CoverageAgent
from claimtriage.agents.tools import InProcessToolBox
from claimtriage.agents.triage import TriageAgent
from claimtriage.config import Settings
from claimtriage.guardrails import scan_inputs
from claimtriage.llm.client import LLMResult, ProviderUnavailable
from claimtriage.samples import base_fnol, food_delivery_fnol
from claimtriage.trace import TraceRecorder
from stub import llm_stub


class FakeBreaker:
    name = "closed"


class FakeLLM:
    breaker = FakeBreaker()

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, 0

    def chat(self, messages, *, agent, step, **kw):
        self.calls += 1
        if self.mode == "down":
            raise ProviderUnavailable("down")
        system = messages[0]["content"]
        fn = llm_stub.coverage_turn if "[AGENT:coverage]" in system else llm_stub.triage_turn if "[AGENT:triage]" in system else None
        if self.mode == "garbage":
            text = "not json"
        else:
            llm_stub.FAULT["mode"] = {"hallu_once": "hallucinate_once", "hallu_always": "hallucinate_always"}.get(self.mode, "none")
            text = json.dumps(fn(messages)) if fn else "{}"
        return LLMResult(text, 100, 20, 0.0, 0.01, "fake")


class LocalToolBox:
    pass


def run(claim, index, llm=None, settings=None):
    s = settings or Settings()
    rec = TraceRecorder("t")
    san = scan_inputs({k: claim[k] for k in ("claim_description", "claimed_damage")}, 0.8, 0.4)["sanitized"]
    llm = llm or FakeLLM()
    ho = TriageAgent(llm, s, rec).run(claim, san, [])
    tb = InProcessToolBox(s, claim, rec, None, index=index)
    return CoverageAgent(llm, s, rec, tb, claim, san, ho).run(), rec, ho, tb


def test_section_4_1_cites_both_cover_and_restriction(index):
    out, rec, ho, tb = run(food_delivery_fnol(), index)
    assert ho["entities"]["use_purpose"] == "commercial_delivery"
    assert out["kind"] == "answer"
    a = out["answer"]
    ids = {c["clause_id"] for c in a["citations"]}
    assert "PCPP:GE:01" in ids and any(i.startswith("PCPP:SI") for i in ids)
    assert a["position"] == "RESTRICTION_REQUIRES_VERIFICATION" and a["within_permitted_use"] == "unverifiable"
    assert all(c["clause_id"] in tb.retrieved for c in a["citations"])      # every citation resolves to retrieved text


def test_agent_chooses_tools_and_searches_for_modifier(index):
    _, rec, _, tb = run(food_delivery_fnol(), index)
    tools = [e["tool"] for e in rec.events if e["type"] == "tool_call"]
    assert tools[0] == "search_policy" and "list_cross_references" in tools and tb.modifier_searched
    # the restriction was found by a query derived from what the agent read, not from the claim wording
    q = [e["args"]["query"] for e in rec.events if e["type"] == "tool_call" and e["args"].get("section_type") == "exclusion"][0]
    assert "limitations as to use" in q.lower() or "otherwise than in accordance" in q.lower()


def test_loop_is_bounded(index):
    s = Settings()
    object.__setattr__(s, "agent_max_tool_calls", 2)
    out, _, _, tb = run(food_delivery_fnol(), index, settings=s)
    assert tb.calls <= 2
    assert out["kind"] in ("answer", "escalation")


def test_hallucinated_citation_caught_then_retried(index):
    out, rec, *_ = run(food_delivery_fnol(), index, llm=FakeLLM("hallu_once"))
    ver = [e for e in rec.events if e["type"] == "verification"]
    assert [v["passed"] for v in ver] == [False, True] and out["kind"] == "answer" and out["attempts"] == 2
    assert "UNRESOLVED_CITATION" in ver[0]["verdict"]["codes"]


def test_persistent_hallucination_escalates_never_returns_unverified(index):
    out, *_ = run(food_delivery_fnol(), index, llm=FakeLLM("hallu_always"))
    assert out["kind"] == "escalation" and out["code"] == "VERIFICATION_FAILED"


def test_provider_down_degrades_to_evidence_pack(index):
    out, rec, ho, _ = run(base_fnol(), index, llm=FakeLLM("down"))
    assert out["kind"] == "escalation" and out["code"] == "PROVIDER_UNAVAILABLE" and out["degraded"]
    assert ho["source"] == "rules" and out["partial"]["evidence"]


def test_garbage_output_escalates(index):
    out, *_ = run(base_fnol(), index, llm=FakeLLM("garbage"))
    assert out["kind"] == "escalation" and out["code"] == "LLM_OUTPUT_INVALID"


def test_out_of_corpus_vehicle_escalates_without_llm_call(index):
    llm = FakeLLM()
    c = base_fnol(vehicle={"vehicle_class": "commercial"})
    rec = TraceRecorder("t")
    san = {"claim_description": c["claim_description"], "claimed_damage": c["claimed_damage"]}
    ho = {"claim_type": "own_damage", "entities": {}, "risk_flags": {"hard": [], "soft": []}, "inconsistencies": []}
    out = CoverageAgent(llm, Settings(), rec, InProcessToolBox(Settings(), c, rec, None, index=index), c, san, ho).run()
    assert out["code"] == "OUT_OF_CORPUS" and llm.calls == 0


def test_unanswerable_claim_escalates_not_fabricates(index):
    c = base_fnol(claim_description="My dog needs hospitalisation after dengue fever and I want the surgery reimbursed by the policy.",
                  claimed_damage="Veterinary hospital bill")
    out, *_ = run(c, index)
    assert out["kind"] == "escalation"
