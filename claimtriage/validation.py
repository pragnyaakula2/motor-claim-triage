"""Ingestion-boundary validation (F2, D1): schema -> range -> category -> cross-field.

Returns *all* field-level errors so a rejected record carries the full list into
quarantine. Nothing here touches the queue or an agent.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from .contracts import FNOL, FieldError


def _loc(err: dict) -> str:
    return ".".join(str(p) for p in err["loc"]) or "$"


def pydantic_errors(exc: ValidationError) -> list[FieldError]:
    out = []
    for e in exc.errors():
        msg = e["msg"].removeprefix("Value error, ")
        out.append(FieldError(field=_loc(e), code=e["type"], message=msg))
    return out


def cross_field_errors(f: FNOL, today: date | None = None) -> list[FieldError]:
    today = today or date.today()
    errs: list[FieldError] = []

    def add(field: str, code: str, msg: str):
        errs.append(FieldError(field=field, code=code, message=msg))

    reported = f.date_reported or today
    if f.policy_end_date <= f.policy_start_date:
        add("policy_end_date", "xfield.policy_period", "policy_end_date must be after policy_start_date")
    elif (f.policy_end_date - f.policy_start_date) > timedelta(days=400):
        add("policy_end_date", "xfield.policy_period_len", "policy period longer than 400 days")
    if f.date_of_loss > today:
        add("date_of_loss", "xfield.future_loss", "date_of_loss is in the future")
    if f.date_of_loss > reported:
        add("date_of_loss", "xfield.loss_after_report", "date_of_loss is after date_reported")
    if reported > today:
        add("date_reported", "xfield.future_report", "date_reported is in the future")
    if not (f.policy_start_date <= f.date_of_loss <= f.policy_end_date):
        add("date_of_loss", "xfield.outside_policy_period", "date_of_loss falls outside the policy period")
    if f.vehicle.year_of_manufacture > f.date_of_loss.year:
        add("vehicle.year_of_manufacture", "xfield.vehicle_year", "vehicle manufactured after date_of_loss")
    if f.estimated_repair_cost_inr is not None and f.estimated_repair_cost_inr > f.vehicle.idv_inr * 1.0 \
            and f.claim_type_declared.value != "total_loss":
        add("estimated_repair_cost_inr", "xfield.cost_exceeds_idv",
            "estimated repair cost exceeds IDV; declare claim_type_declared=total_loss instead")
    if f.police_report.filed and not f.police_report.fir_number:
        add("police_report.fir_number", "xfield.fir_missing", "fir_number is required when police_report.filed is true")
    if f.police_report.fir_number and not f.police_report.filed:
        add("police_report.filed", "xfield.fir_without_flag", "fir_number supplied but police_report.filed is false")
    if f.claim_type_declared.value == "theft" and f.vehicle.drivable:
        add("vehicle.drivable", "xfield.theft_drivable", "a stolen vehicle cannot be marked drivable")
    if not f.injuries_reported and f.injured_count != 0:
        add("injured_count", "xfield.injury_count", "injured_count must be 0 when injuries_reported is false")
    if f.injuries_reported and f.injured_count == 0:
        add("injured_count", "xfield.injury_count", "injured_count must be >= 1 when injuries_reported is true")
    if not f.third_party_involved and f.third_party_damage_inr > 0:
        add("third_party_damage_inr", "xfield.tp_damage", "third_party_damage_inr > 0 requires third_party_involved=true")
    if f.claim_type_declared.value == "third_party" and not f.third_party_involved:
        add("third_party_involved", "xfield.tp_claim_without_tp", "claim_type_declared=third_party requires third_party_involved=true")
    return errs


def validate_fnol(payload: Any, today: date | None = None) -> tuple[FNOL | None, list[FieldError]]:
    """Return (model, []) when valid, else (None, all errors)."""
    if not isinstance(payload, dict):
        return None, [FieldError(field="$", code="type.object", message="request body must be a JSON object")]
    try:
        model = FNOL.model_validate(payload)
    except ValidationError as exc:
        return None, pydantic_errors(exc)
    errs = cross_field_errors(model, today)
    return (None, errs) if errs else (model, [])
