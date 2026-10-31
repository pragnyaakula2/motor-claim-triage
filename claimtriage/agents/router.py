"""F6: route to fast_track | surveyor_assignment | investigation, always with a stated reason.

Deterministic, ordered rules so that every routing decision is explainable and testable. The model
informs the *coverage position*; it does not pick the queue.
Thresholds come from configuration (FAST_TRACK_MAX_INR default mirrors the 'up to Rs.20,000' minor-damage
fast-settlement note in the claims-procedure document).
"""
from __future__ import annotations

from ..config import Settings
from ..guardrails import ALLOWED_POSITIONS  # noqa: F401  (kept for symmetry / import checks)

RESTRICTION_POSITIONS = {"RESTRICTION_LIKELY_APPLIES", "RESTRICTION_REQUIRES_VERIFICATION", "CONDITION_NOT_MET_RISK"}


def route_claim(claim: dict, handoff: dict, position: str | None, settings: Settings) -> dict:
    ent = handoff.get("entities", {})
    flags = handoff.get("risk_flags", {"hard": [], "soft": []})
    sec = handoff.get("security_flags", [])
    ctype = handoff["claim_type"]
    cost = claim.get("estimated_repair_cost_inr")

    def out(route: str, rule: str, reason: str):
        return {"route": route, "rule": rule, "reason": reason}

    if sec:
        return out("investigation", "R1_security", f"Input contained instruction-like or abusive content ({', '.join(sec[:3])}); needs human scrutiny of the narrative.")
    hard = list(flags.get("hard", []))
    if ent.get("intoxication_mentioned"):
        hard.append("intoxication_mentioned")
    if handoff.get("inconsistencies"):
        hard_inc = [i for i in handoff["inconsistencies"] if i.startswith("declared claim type") or "paid-delivery" in i]
        hard += hard_inc
    soft = list(flags.get("soft", []))
    if hard or len(soft) >= 2:
        return out("investigation", "R2_risk_flags", "Risk indicators: " + "; ".join(hard + soft))
    if position in RESTRICTION_POSITIONS:
        return out("investigation", "R3_restriction", "Coverage position indicates a policy restriction/condition may defeat or limit cover (e.g. permitted-use or condition breach); facts need investigation before any decision.")
    if ctype == "theft":
        return out("investigation", "R4_theft", "Theft claims require police/FIR verification and investigation.")
    if ctype == "third_party" or claim.get("injuries_reported") or claim.get("third_party_involved"):
        return out("surveyor_assignment", "R5_third_party", "Third-party exposure (property damage or injury) requires assessment by a surveyor.")
    if ctype == "total_loss":
        return out("surveyor_assignment", "R6_total_loss", "Total/constructive total loss requires surveyor assessment against IDV.")
    if not claim["vehicle"]["drivable"]:
        return out("surveyor_assignment", "R7_not_drivable", "Vehicle is not drivable; surveyor inspection needed.")
    if cost is None:
        return out("surveyor_assignment", "R8_no_estimate", "No repair estimate supplied; surveyor needed to assess the damage.")
    if cost > settings.fast_track_max_inr:
        return out("surveyor_assignment", "R9_above_threshold", f"Estimated repair Rs.{cost:,.0f} exceeds the fast-track limit Rs.{settings.fast_track_max_inr:,.0f}.")
    if ctype == "own_damage" and (position == "PRIMA_FACIE_WITHIN_COVER"):
        return out("fast_track", "R10_fast_track", f"Own-damage claim, estimate Rs.{cost:,.0f} within the Rs.{settings.fast_track_max_inr:,.0f} limit, no risk flags, and no restriction found on the retrieved wording.")
    return out("surveyor_assignment", "R11_default", "No rule allows fast-tracking; defaulting to surveyor assessment.")
