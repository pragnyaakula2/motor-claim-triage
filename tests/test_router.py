from claimtriage.config import get_settings
from claimtriage.agents.router import route_claim
from claimtriage.samples import base_fnol

S = get_settings()


def ho(**kw):
    d = {"claim_type": "own_damage", "entities": {"use_purpose": "private", "intoxication_mentioned": False},
         "inconsistencies": [], "risk_flags": {"hard": [], "soft": []}, "security_flags": []}
    d.update(kw)
    return d


def r(claim=None, handoff=None, pos="PRIMA_FACIE_WITHIN_COVER"):
    return route_claim(claim or base_fnol(), handoff or ho(), pos, S)["route"]


def test_fast_track():
    assert r() == "fast_track"


def test_above_threshold():
    assert r(base_fnol(estimated_repair_cost_inr=95000)) == "surveyor_assignment"


def test_restriction_goes_to_investigation():
    assert r(pos="RESTRICTION_REQUIRES_VERIFICATION") == "investigation"


def test_security_flag_goes_to_investigation():
    assert r(handoff=ho(security_flags=["injection:persona_switch"])) == "investigation"


def test_intoxication():
    assert r(handoff=ho(entities={"use_purpose": "private", "intoxication_mentioned": True})) == "investigation"


def test_two_soft_flags():
    assert r(handoff=ho(risk_flags={"hard": [], "soft": ["late_report:9d", "early_loss:3d"]})) == "investigation"


def test_one_soft_flag_ok():
    assert r(handoff=ho(risk_flags={"hard": [], "soft": ["late_report:9d"]})) == "fast_track"


def test_theft_and_total_loss():
    assert r(handoff=ho(claim_type="theft")) == "investigation"
    assert r(handoff=ho(claim_type="total_loss")) == "surveyor_assignment"


def test_third_party():
    assert r(base_fnol(third_party_involved=True, third_party_damage_inr=1000), ho(claim_type="third_party")) == "surveyor_assignment"
