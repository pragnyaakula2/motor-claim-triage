"""Triage Agent (agent 1 of 2): classify the claim and extract entities (F3).

Distinct responsibility from the Coverage Agent: it never reads the policy. Its output is the typed
hand-off ``TriageHandoff`` consumed by the Coverage Agent and by the router.
Has a deterministic rules baseline so that a provider outage degrades (rather than fails) this stage.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date

from .. import metrics as M
from ..config import Settings
from ..guardrails import sanitize_text
from ..llm.client import LLMBadResponse, LLMClient, ProviderUnavailable
from ..llm.prompts import load_prompt
from ..telemetry import span
from ..trace import TraceRecorder
from .common import parse_json_object

log = logging.getLogger("triage")
CLAIM_TYPES = {"own_damage", "third_party", "theft", "total_loss"}
PURPOSES = {"private", "commercial_delivery", "hire_or_reward", "business", "unknown"}

DELIVERY_RE = re.compile(r"\b(food[\s-]?delivery|deliver(?:y|ing|ed)|zomato|swiggy|dunzo|blinkit|zepto|courier|parcel|doordash|uber[\s-]?eats|porter)\b", re.I)
HIRE_RE = re.compile(r"\b(uber|ola|rapido|cab|taxi|ride[\s-]?(?:hail|share|sharing)|carpool for (?:a )?fee|passengers?\b.{0,30}\b(?:fare|paid|payment|charged)|paid passengers?|for hire|rental)\b", re.I)
BUSINESS_RE = re.compile(r"\b(client meeting|business trip|site visit|office work)\b", re.I)
PAID_RE = re.compile(r"\b(paid|earn(?:ing|ed|s)?|per[\s-]?trip|per[\s-]?order|commission|gig|side[\s-]?income|fare|incentive|payout)\b", re.I)
INTOX_RE = re.compile(r"\b(drunk|drinking|alcohol|intoxicat|under the influence|had (?:a )?few drinks|beer|whisk(?:e)?y|liquor|breath ?analy[sz]er)\b", re.I)
THEFT_RE = re.compile(r"\b(stolen|theft|robbed|robbery|snatched|carjack|missing from (?:the )?parking|vehicle (?:was )?gone|break[\s-]?in|burglary)\b", re.I)
TP_RE = re.compile(r"\b(third[\s-]?party|pedestrian|cyclist|(?:i|we) (?:hit|rammed|struck|knocked|crashed into)|(?:hit|rammed|struck|knocked|crashed into) (?:another|an?|the|their) (?:car|vehicle|bike|scooter|person|man|woman|wall|gate|shop|pole belonging)|their (?:car|vehicle|property)|damaged (?:a|the|their) (?:wall|gate|shop|property))\b", re.I)
TOTAL_RE = re.compile(r"\b(total loss|totalled|totaled|write[\s-]?off|burnt (?:down|out)|burned (?:down|out)|beyond repair|completely (?:destroyed|submerged)|washed away|gutted)\b", re.I)


def rules_classify(claim: dict) -> dict:
    text = f'{claim["claim_description"]} {claim["claimed_damage"]}'
    declared = claim.get("claim_type_declared", "unknown")
    scores = {"theft": 0, "total_loss": 0, "third_party": 0, "own_damage": 1}
    if THEFT_RE.search(text):
        scores["theft"] += 3
    if TOTAL_RE.search(text):
        scores["total_loss"] += 3
    if TP_RE.search(text) or claim.get("third_party_involved"):
        scores["third_party"] += 2 if claim.get("third_party_damage_inr", 0) > 0 or claim.get("injuries_reported") else 1
    if declared in scores:
        scores[declared] += 1
    ctype = max(scores, key=lambda k: (scores[k], k == "own_damage"))
    # own damage + third party both present: keep the dominant one, flag the other as entity
    top = sorted(scores.values(), reverse=True)
    conf = 0.55 + 0.1 * (top[0] - top[1]) if len(top) > 1 else 0.6
    if DELIVERY_RE.search(text) and PAID_RE.search(text) or DELIVERY_RE.search(text):
        purpose = "commercial_delivery"
    elif HIRE_RE.search(text):
        purpose = "hire_or_reward"
    elif BUSINESS_RE.search(text):
        purpose = "business"
    elif re.search(r"\b(commut|going to (?:work|office)|grocer|shopping|family|personal|weekend|my (?:wife|husband|kids?|son|daughter))\b", text, re.I):
        purpose = "private"
    else:
        purpose = "unknown"
    return {
        "claim_type": ctype, "confidence": round(min(conf, 0.9), 2),
        "entities": {
            "use_purpose": purpose,
            "paid_activity_mentioned": bool(PAID_RE.search(text)) or purpose in ("commercial_delivery", "hire_or_reward"),
            "intoxication_mentioned": bool(INTOX_RE.search(text)),
            "third_party_injury": bool(claim.get("injuries_reported")),
            "driver_is_owner": bool(claim["driver"]["is_owner"]),
            "damage_summary": claim["claimed_damage"][:160],
        },
        "inconsistencies": [], "suspected_injection": False,
    }


MOTOR_RE = re.compile(r"\b(car|cars|vehicle|auto|sedan|suv|hatchback|bumper|windshield|windscreen|headlamp|headlight|tail ?lamp|tyre|tire|wheel|door|bonnet|hood|engine|radiator|dent(?:ed)?|scratch(?:ed)?|collision|collided|accident|crash(?:ed)?|hit|rammed|reversed|parked|driving|drove|driver|road|highway|flyover|divider|traffic|skid(?:ded)?|flood(?:ed)?|submerged|fire|burnt|stolen|theft|towed|garage|repair|mechanic|airbag|steering|brake|clutch|gearbox|chassis|fender|mirror|glass|pole|tree|pothole|truck|bus|bike|scooter|cyclist|pedestrian)\b", re.I)
OUT_OF_WORDING_RE = re.compile(r"\b(zero[\s-]?dep(?:reciation)?|engine (?:protect(?:or|ion)?|and gear ?box protect(?:or|ion)?)|return[\s-]to[\s-]invoice|\bRTI\b|roadside assistance|consumables?|key replacement|loss of use|downtime protection|rental car|courtesy car|no[\s-]claim[\s-]bonus protect(?:ion)?|tyre protect(?:ion)?|battery protect(?:ion)?)\b", re.I)


def scope_check(claim: dict) -> dict:
    """Deterministic scope guard (F7): is this a motor-vehicle loss, and is it a topic the indexed wording addresses?"""
    text = f'{claim["claim_description"]} {claim["claimed_damage"]}'
    n = len(MOTOR_RE.findall(text))
    topics = sorted({m.group(0).lower() for m in OUT_OF_WORDING_RE.finditer(text)})
    return {"motor_terms": n, "out_of_wording_topics": topics, "in_scope": n > 0 and not topics}


def structural_flags(claim: dict, settings: Settings) -> dict:
    """Deterministic risk signals from the structured fields (no model involved)."""
    hard, soft = [], []
    loss = date.fromisoformat(claim["date_of_loss"])
    rep = date.fromisoformat(claim["date_reported"]) if claim.get("date_reported") else date.today()
    start = date.fromisoformat(claim["policy_start_date"])
    if (rep - loss).days > settings.late_report_days:
        soft.append(f"late_report:{(rep - loss).days}d")
    if (loss - start).days <= 30:
        soft.append(f"early_loss:{(loss - start).days}d_after_inception")
    if claim["driver"].get("licence_valid") is False:
        hard.append("driver_licence_invalid")
    if claim["driver"].get("licence_valid") is None:
        soft.append("driver_licence_unknown")
    if claim.get("claim_type_declared") == "theft" and not claim["police_report"]["filed"]:
        hard.append("theft_without_police_report")
    cost = claim.get("estimated_repair_cost_inr")
    if cost and cost > 0.75 * claim["vehicle"]["idv_inr"] and claim.get("claim_type_declared") != "total_loss":
        soft.append("cost_near_total_loss_threshold")
    return {"hard": hard, "soft": soft}


class TriageAgent:
    name = "triage"

    def __init__(self, llm: LLMClient, settings: Settings, recorder: TraceRecorder):
        self.llm, self.s, self.rec = llm, settings, recorder

    def run(self, claim: dict, sanitized: dict, security_flags: list[str]) -> dict:
        t0 = time.time()
        prompt = load_prompt("triage", self.s.prompt_triage)
        base = rules_classify(claim)
        flags = structural_flags(claim, self.s)
        source, degraded = "rules", False
        llm_out: dict | None = None
        claim_view = {
            "claim_type_declared": claim["claim_type_declared"],
            "claim_description": sanitized["claim_description"],
            "claimed_damage": sanitized["claimed_damage"],
            "third_party_involved": claim["third_party_involved"],
            "injuries_reported": claim["injuries_reported"],
            "driver_is_owner": claim["driver"]["is_owner"],
        }
        msgs = [{"role": "system", "content": prompt.text},
                {"role": "user", "content": "<claim_json>" + json.dumps(claim_view, ensure_ascii=True) + "</claim_json>\nClassify."}]
        with span("agent.triage"):
            for attempt in range(2):
                ts = time.time()
                try:
                    res = self.llm.chat(msgs, agent=self.name, step="classify", max_tokens=400)
                except ProviderUnavailable as e:
                    degraded = True
                    self.rec.emit(self.name, "llm_call", "classify", ok=False, error=str(e)[:160], degraded=True,
                                  prompt_version=prompt.id, latency_s=round(time.time() - ts, 3))
                    M.DEGRADED.labels("triage_rules").inc()
                    break
                except LLMBadResponse as e:
                    self.rec.emit(self.name, "llm_call", "classify", ok=False, error=str(e)[:160], prompt_version=prompt.id)
                    degraded = True
                    break
                ev = dict(ok=True, prompt_version=prompt.id, latency_s=round(res.latency_s, 3), prompt_tokens=res.prompt_tokens,
                          completion_tokens=res.completion_tokens, cost_usd=round(res.cost_usd, 6), model=res.model,
                          attempts=res.attempts)
                try:
                    llm_out = self._validate(parse_json_object(res.text))
                    self.rec.emit(self.name, "llm_call", "classify", **ev)
                    source = "llm"
                    break
                except (ValueError, KeyError) as e:
                    self.rec.emit(self.name, "llm_call", "classify", parse_error=str(e)[:120], **{**ev, "ok": False})
                    M.RETRIES.labels(self.name, "parse").inc()
                    msgs.append({"role": "assistant", "content": res.text[:500]})
                    msgs.append({"role": "user", "content": f"INVALID_OUTPUT: {e}. Reply with ONE JSON object in the required schema."})
        out = llm_out or base
        inconsistencies = list(out.get("inconsistencies", []))
        if llm_out and llm_out["claim_type"] != base["claim_type"]:
            inconsistencies.append(f"classifier disagreement: model={llm_out['claim_type']} rules={base['claim_type']}")
        declared = claim["claim_type_declared"]
        if declared != "unknown" and out["claim_type"] != declared:
            inconsistencies.append(f"declared claim type '{declared}' differs from narrative-derived '{out['claim_type']}'")
        if out["entities"].get("suspected_injection") or out.get("suspected_injection"):
            security_flags = security_flags + ["model_reported_injection"]
        # the rules signals for intoxication / purpose are conservative: keep the stricter reading
        ent = out["entities"]
        if base["entities"]["intoxication_mentioned"]:
            ent["intoxication_mentioned"] = True
        if base["entities"]["use_purpose"] in ("commercial_delivery", "hire_or_reward") and ent["use_purpose"] in ("private", "unknown"):
            ent["use_purpose"] = base["entities"]["use_purpose"]
            inconsistencies.append("model reported private/unknown use but narrative contains paid-delivery or hire indicators")
        handoff = {
            "claim_type": out["claim_type"], "confidence": float(out.get("confidence", 0.5)), "entities": ent,
            "inconsistencies": inconsistencies, "risk_flags": flags, "security_flags": security_flags,
            "source": source, "degraded": degraded, "prompt_version": prompt.id,
            "scope": scope_check(claim),
        }
        dt = time.time() - t0
        M.AGENT_RUN_LATENCY.labels(self.name).observe(dt)
        M.RUNS.labels(self.name, "degraded" if degraded else "completed").inc()
        M.RUN_STEPS.labels(self.name).observe(1)
        M.RUN_TOOLCALLS.labels(self.name).observe(0)
        self.rec.emit(self.name, "handoff", "triage_to_coverage", handoff=handoff, latency_s=round(dt, 3))
        return handoff

    @staticmethod
    def _validate(d: dict) -> dict:
        ct = d["claim_type"]
        if ct not in CLAIM_TYPES:
            raise ValueError(f"claim_type {ct!r} not in {sorted(CLAIM_TYPES)}")
        ent = d.get("entities") or {}
        purpose = ent.get("use_purpose", "unknown")
        if purpose not in PURPOSES:
            raise ValueError(f"use_purpose {purpose!r} not in {sorted(PURPOSES)}")
        conf = float(d.get("confidence", 0.5))
        return {
            "claim_type": ct, "confidence": max(0.0, min(1.0, conf)),
            "entities": {
                "use_purpose": purpose, "paid_activity_mentioned": bool(ent.get("paid_activity_mentioned", False)),
                "intoxication_mentioned": bool(ent.get("intoxication_mentioned", False)),
                "third_party_injury": bool(ent.get("third_party_injury", False)),
                "driver_is_owner": bool(ent.get("driver_is_owner", True)),
                "damage_summary": sanitize_text(str(ent.get("damage_summary", "")))[:160],
                "suspected_injection": bool(d.get("suspected_injection", False)),
            },
            "inconsistencies": [sanitize_text(str(x))[:200] for x in (d.get("inconsistencies") or [])][:6],
            "suspected_injection": bool(d.get("suspected_injection", False)),
        }
