
from typing import Any


def analyze_evidence(
    claim_description: str,
    claimed_damage: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Produce a preliminary, evidence-linked claim assessment.
    This is a rule-based baseline, not an automated coverage decision.
    """

    relevant_terms = {
        "damage", "collision", "accident", "repair",
        "exclusion", "deductible", "insured", "loss",
        "coverage", "claim",
    }

    ranked_evidence = []

    for item in evidence:
        text = item.get("text", "").lower()
        matches = sum(1 for term in relevant_terms if term in text)

        ranked_evidence.append({
            **item,
            "relevance_matches": matches,
        })

    ranked_evidence.sort(
        key=lambda item: (
            item["relevance_matches"],
            item.get("score", 0),
        ),
        reverse=True,
    )

    return {
        "claim_description": claim_description,
        "claimed_damage": claimed_damage,
        "assessment": "REQUIRES_HUMAN_REVIEW",
        "reason": (
            "Retrieved policy passages require verification against "
            "the applicable coverage section, exclusions, and policy schedule."
        ),
        "evidence": ranked_evidence,
        "limitations": [
            "Keyword matching does not establish legal applicability.",
            "Similarity scores do not prove coverage.",
            "A human reviewer must verify the cited clauses."
        ],
    }

