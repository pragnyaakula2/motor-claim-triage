from datetime import date, timedelta

import pytest

from claimtriage.samples import base_fnol, food_delivery_fnol
from claimtriage.validation import validate_fnol


def codes(payload):
    m, errs = validate_fnol(payload)
    return m, {e.code for e in errs}, {e.field for e in errs}


def test_valid_payload_passes():
    m, c, _ = codes(base_fnol())
    assert m is not None and not c


def test_food_delivery_sample_valid():
    assert codes(food_delivery_fnol())[0] is not None


def test_not_an_object():
    assert validate_fnol([1, 2])[0] is None


def test_all_field_errors_reported_together():
    p = base_fnol(vehicle={"registration": "XX", "year_of_manufacture": 1950, "vehicle_class": "tank"}, contact_phone="12")
    _, _, fields = codes(p)
    assert {"vehicle.registration", "vehicle.year_of_manufacture", "vehicle.vehicle_class", "contact_phone"} <= fields


def test_unknown_field_rejected():
    _, c, _ = codes({**base_fnol(), "approve": True})
    assert "extra_forbidden" in c


def test_bad_state_category():
    _, _, f = codes(base_fnol(location={"state": "Atlantis"}))
    assert "location.state" in f


@pytest.mark.parametrize("mut,code", [
    ({"date_of_loss": (date.today() + timedelta(days=3)).isoformat()}, "xfield.future_loss"),
    ({"date_of_loss": (date.today() - timedelta(days=400)).isoformat()}, "xfield.outside_policy_period"),
    ({"policy_end_date": (date.today() - timedelta(days=300)).isoformat()}, "xfield.policy_period"),
    ({"estimated_repair_cost_inr": 9_000_000}, "xfield.cost_exceeds_idv"),
    ({"police_report": {"filed": True}}, "xfield.fir_missing"),
    ({"police_report": {"filed": False, "fir_number": "FIR-1"}}, "xfield.fir_without_flag"),
    ({"claim_type_declared": "theft"}, "xfield.theft_drivable"),
    ({"injured_count": 2}, "xfield.injury_count"),
    ({"third_party_damage_inr": 500}, "xfield.tp_damage"),
    ({"claim_type_declared": "third_party"}, "xfield.tp_claim_without_tp"),
])
def test_cross_field_rules(mut, code):
    _, c, _ = codes(base_fnol(**mut))
    assert code in c


def test_total_loss_may_exceed_idv():
    _, c, _ = codes(base_fnol(estimated_repair_cost_inr=9_000_000, claim_type_declared="total_loss"))
    assert "xfield.cost_exceeds_idv" not in c
