from claimtriage.agents.verifier import verify_answer

R = {
    "PCPP:SI:01": {"clause_id": "PCPP:SI:01", "text": "The Company will indemnify the insured against loss or damage to the vehicle by accidental external means; subject to depreciation.", "section_type": "cover,condition", "section": "Section I", "score": 0.7},
    "PCPP:GE:01": {"clause_id": "PCPP:GE:01", "text": "The Company shall not be liable whilst the vehicle is being used otherwise than in accordance with the Limitations as to Use of up to 75% of the IDV.", "section_type": "exclusion", "section": "General Exceptions", "score": 0.6},
}
CLAIM = {"claim_description": "food delivery run", "claimed_damage": "bumper", "estimated_repair_cost_inr": 18000, "vehicle": {"idv_inr": 520000}}
HO = {"entities": {"use_purpose": "commercial_delivery"}}
GOOD = {"position": "RESTRICTION_REQUIRES_VERIFICATION", "summary": "Cover under Section I but General Exceptions limits it.", "within_permitted_use": "unverifiable",
        "restriction_assessment": [{"clause_id": "PCPP:GE:01", "effect": "excludes", "applies": "unverified", "reason": "x"}],
        "citations": [{"clause_id": "PCPP:SI:01", "role": "cover", "quote": "indemnify the insured against loss or damage to the vehicle by accidental external means"},
                      {"clause_id": "PCPP:GE:01", "role": "restriction", "quote": "being used otherwise than in accordance with the Limitations as to Use"}]}


def v(ans, mod=True, best=0.6, ho=HO):
    return verify_answer(ans, R, handoff=ho, claim=CLAIM, modifier_searched=mod, best_score=best, weak_threshold=0.25)


def test_good_answer_passes():
    assert v(GOOD).passed


def test_fabricated_clause_id_caught():
    bad = {**GOOD, "citations": GOOD["citations"] + [{"clause_id": "PCPP:GE:99", "role": "restriction", "quote": "made up text here ok"}]}
    r = v(bad)
    assert not r.passed and "UNRESOLVED_CITATION" in r.codes


def test_non_verbatim_quote_caught():
    bad = {**GOOD, "citations": [GOOD["citations"][0], {"clause_id": "PCPP:GE:01", "role": "restriction", "quote": "expressly excludes food delivery for payment"}]}
    assert "UNSUPPORTED_QUOTE" in v(bad).codes


def test_unsupported_figure_caught():
    assert "UNSUPPORTED_FIGURE" in v({**GOOD, "summary": "Payout limited to Rs. 99,999."}).codes


def test_supported_figure_ok():
    assert v({**GOOD, "summary": "Total loss threshold of 75% applies to the IDV."}).passed


def test_modifiers_not_examined_caught():
    assert "MODIFIERS_NOT_EXAMINED" in v(GOOD, mod=False).codes


def test_commercial_use_without_restriction_citation_caught():
    only_cover = {**GOOD, "position": "PRIMA_FACIE_WITHIN_COVER", "citations": [GOOD["citations"][0]], "restriction_assessment": []}
    assert "USE_RESTRICTION_NOT_ADDRESSED" in v(only_cover).codes


def test_restriction_position_needs_restriction_citation():
    c = {**GOOD, "citations": [GOOD["citations"][0]]}
    assert "NO_RESTRICTION_CITATION" in v(c, ho={"entities": {"use_purpose": "private"}}).codes


def test_weak_retrieval_caught():
    assert "WEAK_RETRIEVAL" in v(GOOD, best=0.1).codes


def test_escalation_answer_needs_no_cover_but_citations_must_be_real():
    esc = {"position": "INSUFFICIENT_EVIDENCE", "needs_escalation": True, "summary": "x", "citations": []}
    assert v(esc).passed
    esc["citations"] = [{"clause_id": "NOPE:1:01", "quote": "fabricated quote text"}]
    assert not v(esc).passed
