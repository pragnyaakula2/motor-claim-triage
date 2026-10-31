from app.agent import analyze_evidence


def test_agent_preserves_claim_details():
    result = analyze_evidence(
        claim_description="Car damaged in a collision",
        claimed_damage="Own damage",
        evidence=[],
    )

    assert result["claim_description"] == "Car damaged in a collision"
    assert result["claimed_damage"] == "Own damage"


def test_agent_always_requires_human_review():
    result = analyze_evidence(
        claim_description="Accident damage",
        claimed_damage="Body damage",
        evidence=[
            {
                "source": "policy.pdf",
                "page": 3,
                "text": "Collision damage and repair conditions",
                "score": 0.8,
            }
        ],
    )

    assert result["assessment"] == "REQUIRES_HUMAN_REVIEW"


def test_agent_returns_evidence():
    evidence = [
        {
            "source": "policy.pdf",
            "page": 3,
            "text": "Collision damage and repair conditions",
            "score": 0.8,
        }
    ]

    result = analyze_evidence(
        claim_description="Collision",
        claimed_damage="Body damage",
        evidence=evidence,
    )

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source"] == "policy.pdf"


def test_agent_includes_limitations():
    result = analyze_evidence(
        claim_description="Accident",
        claimed_damage="Damage",
        evidence=[],
    )

    assert len(result["limitations"]) > 0

def test_relevant_evidence_is_ranked_higher():
    evidence = [
        {
            "source": "irrelevant.pdf",
            "page": 2,
            "text": "General information about the company office.",
            "score": 0.9,
        },
        {
            "source": "policy.pdf",
            "page": 5,
            "text": (
                "Collision damage, accidental loss, "
                "vehicle repair and insurance coverage."
            ),
            "score": 0.5,
        },
    ]

    result = analyze_evidence(
        claim_description="Car damaged in a collision",
        claimed_damage="Own damage",
        evidence=evidence,
    )

    assert result["evidence"][0]["source"] == "policy.pdf"
