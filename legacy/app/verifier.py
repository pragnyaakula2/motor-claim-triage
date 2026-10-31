
from typing import Any


def verify_assessment(
    assessment: dict[str, Any],
) -> dict[str, Any]:
    evidence = assessment.get("evidence", [])
    issues = []

    if not evidence:
        issues.append("No policy evidence was retrieved.")

    for item in evidence:
        if not item.get("source"):
            issues.append("An evidence item has no source filename.")

        if not isinstance(item.get("page"), int):
            issues.append("An evidence item has no valid page number.")

        if not item.get("text", "").strip():
            issues.append("An evidence item contains no policy text.")

    if assessment.get("assessment") != "REQUIRES_HUMAN_REVIEW":
        issues.append(
            "The baseline assessment must not make an automatic "
            "coverage decision."
        )

    verified = not issues

    return {
        "verification_status": (
            "EVIDENCE_STRUCTURE_VALID"
            if verified
            else "VERIFICATION_FAILED"
        ),
        "verified": verified,
        "issues": issues,
        "coverage_decision": "HUMAN_REVIEW_REQUIRED",
        "note": (
            "This verifier checks evidence structure and safe handling. "
            "It does not establish legal coverage or confirm that the "
            "retrieved passages support every claim."
        ),
    }

