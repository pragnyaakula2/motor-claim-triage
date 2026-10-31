
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent import analyze_evidence
from app.retrieve import PolicyRetriever
from app.verifier import verify_assessment

app = FastAPI(
    title="Motor Insurance Claim Triage",
    description="Retrieves policy evidence for motor insurance claims.",
    version="0.3.0",
)

retriever = None


class ClaimRequest(BaseModel):
    claim_id: str = Field(min_length=1)
    policy_number: str = Field(min_length=1)
    vehicle_registration: str = Field(min_length=1)
    date_of_loss: str = Field(min_length=1)
    claim_description: str = Field(min_length=10)
    claimed_damage: str = Field(min_length=1)


@app.on_event("startup")
def load_retriever():
    global retriever

    try:
        retriever = PolicyRetriever()
    except Exception as exc:
        raise RuntimeError(
            f"Could not load policy search index: {exc}"
        ) from exc


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "motor-claim-triage",
    }


def relevance_bonus(
    item: dict,
    claim_description: str,
    claimed_damage: str,
) -> float:
    """Boost policy passages that match the actual claim scenario."""

    text = item.get("text", "").lower()
    description = claim_description.lower()
    damage = claimed_damage.lower()
    claim_text = f"{description} {damage}"

    bonus = 0.0

    # Detect commercial or paid vehicle use.
    commercial_terms = (
        "food delivery",
        "delivery for payment",
        "commercial use",
        "hire or reward",
        "paid passengers",
        "carrying passengers for payment",
        "passenger transport for payment",
    )

    commercial_claim = any(
        term in claim_text for term in commercial_terms
    )

    # Commercial-use claims need clauses about vehicle use restrictions.
    if commercial_claim:
        if "limitations as to use" in text:
            bonus += 0.25

        if "used otherwise than" in text and "use" in text:
            bonus += 0.20

        if "hire or reward" in text:
            bonus += 0.20

        if "private car" in text and (
            "commercial" in text
            or "hire or reward" in text
            or "reward" in text
        ):
            bonus += 0.10

    # Boost accidental-damage coverage clauses for collision claims.
    collision_terms = (
        "collision",
        "collided",
        "crash",
        "accident",
        "impact",
        "struck",
    )

    collision_claim = any(
        term in claim_text for term in collision_terms
    )

    if collision_claim:
        if "accidental external means" in text:
            bonus += 0.20

        if "own damage" in text:
            bonus += 0.15

        if "loss or damage" in text:
            bonus += 0.10

    # Boost theft-related provisions for theft claims.
    theft_terms = ("theft", "stolen", "robbery", "vehicle missing")

    theft_claim = any(term in claim_text for term in theft_terms)

    if theft_claim:
        if "theft" in text or "stolen" in text:
            bonus += 0.25

        if "loss or damage" in text:
            bonus += 0.10

    # Boost water-damage evidence for flooding or heavy rain.
    water_terms = (
        "flood",
        "flooding",
        "water damage",
        "heavy rain",
        "water entered",
    )

    water_claim = any(term in claim_text for term in water_terms)

    if water_claim:
        if any(
            term in text
            for term in ("flood", "inundation", "water damage")
        ):
            bonus += 0.20

        if "own damage" in text:
            bonus += 0.10

    # Personal-accident clauses alone are not vehicle-damage evidence.
    personal_accident_only = (
        "personal accident" in text
        and not any(
            term in text
            for term in (
                "own damage",
                "loss or damage",
                "limitations as to use",
                "hire or reward",
            )
        )
    )

    if personal_accident_only:
        bonus -= 0.15

    return bonus


def retrieve_claim_evidence(
    claim_description: str,
    claimed_damage: str,
    top_k_per_query: int = 5,
) -> list[dict]:
    """Retrieve, deduplicate, and rerank policy evidence."""

    if retriever is None:
        raise HTTPException(
            status_code=503,
            detail="Policy search index is not ready.",
        )

    queries = [
        (
            f"Coverage provisions for this claim: {claim_description}. "
            f"Claimed damage: {claimed_damage}. "
            "Find applicable coverage and accidental damage clauses."
        ),
        (
            f"Claim: {claim_description}. Damage: {claimed_damage}. "
            "Find policy exclusions, exceptions, and restrictions."
        ),
        (
            f"Claim: {claim_description}. Damage: {claimed_damage}. "
            "Find applicable deductibles, conditions, and policy requirements."
        ),
        (
            f"Claim: {claim_description}. Damage: {claimed_damage}. "
            "Find permitted vehicle use and restrictions on commercial use, "
            "hire or reward, and vehicle use conditions."
        ),
    ]

    unique_evidence = {}

    for query in queries:
        results = retriever.search(
            query,
            top_k=top_k_per_query,
        )

        for item in results:
            key = (
                item.get("source"),
                item.get("page"),
                item.get("chunk_id"),
            )

            if key not in unique_evidence:
                unique_evidence[key] = {
                    **item,
                    "retrieval_queries": [query],
                }
            else:
                existing = unique_evidence[key]

                if query not in existing["retrieval_queries"]:
                    existing["retrieval_queries"].append(query)

                if item.get("score", 0) > existing.get("score", 0):
                    existing["score"] = item["score"]

    ranked = list(unique_evidence.values())

    for item in ranked:
        bonus = relevance_bonus(
            item=item,
            claim_description=claim_description,
            claimed_damage=claimed_damage,
        )

        item["ranking_score"] = round(
            item.get("score", 0) + bonus,
            4,
        )

    return sorted(
        ranked,
        key=lambda item: item.get("ranking_score", 0),
        reverse=True,
    )


@app.post("/claims/analyze")
def analyze_claim(claim: ClaimRequest):
    evidence = retrieve_claim_evidence(
        claim_description=claim.claim_description,
        claimed_damage=claim.claimed_damage,
    )

    assessment = analyze_evidence(
        claim_description=claim.claim_description,
        claimed_damage=claim.claimed_damage,
        evidence=evidence,
    )

    verification = verify_assessment(assessment)

    return {
        "claim_id": claim.claim_id,
        "status": "assessment_completed",
        "assessment": assessment,
        "verification": verification,
        "retrieval_summary": {
            "query_count": 4,
            "unique_evidence_count": len(evidence),
        },
    }
