
from app.verifier import verify_assessment


def make_assessment(evidence):
    return {
        "assessment": "REQUIRES_HUMAN_REVIEW",
        "evidence": evidence,
    }


def test_valid_evidence_structure():
    evidence = [
        {
            "source": "Private-Car-Package-Policy.pdf",
            "page": 6,
            "text": "The company shall not be liable under the stated conditions.",
            "score": 0.64,
        }
    ]

    result = verify_assessment(make_assessment(evidence))

    assert result["verified"] is True
    assert result["verification_status"] == "EVIDENCE_STRUCTURE_VALID"
    assert result["coverage_decision"] == "HUMAN_REVIEW_REQUIRED"


def test_empty_evidence_fails():
    result = verify_assessment(make_assessment([]))

    assert result["verified"] is False
    assert "No policy evidence was retrieved." in result["issues"]


def test_missing_source_fails():
    evidence = [
        {
            "page": 2,
            "text": "Some policy wording.",
        }
    ]

    result = verify_assessment(make_assessment(evidence))

    assert result["verified"] is False
    assert any("source filename" in issue for issue in result["issues"])


def test_empty_policy_text_fails():
    evidence = [
        {
            "source": "policy.pdf",
            "page": 2,
            "text": "   ",
        }
    ]

    result = verify_assessment(make_assessment(evidence))

    assert result["verified"] is False
    assert any("no policy text" in issue for issue in result["issues"])


def test_automatic_decision_is_flagged():
    assessment = make_assessment([
        {
            "source": "policy.pdf",
            "page": 1,
            "text": "Policy wording.",
        }
    ])
    assessment["assessment"] = "APPROVED"

    result = verify_assessment(assessment)

    assert result["verified"] is False
    assert result["coverage_decision"] == "HUMAN_REVIEW_REQUIRED"

