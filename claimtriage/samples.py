"""Sample FNOL payloads used by tests, the evaluation set, the demo and the web client."""
from __future__ import annotations

import copy
from datetime import date, timedelta


def base_fnol(**over) -> dict:
    today = date.today()
    d = {
        "client_reference": "demo-001",
        "policy_number": "2311 2001 0042 000",
        "policy_start_date": (today - timedelta(days=200)).isoformat(),
        "policy_end_date": (today + timedelta(days=165)).isoformat(),
        "date_of_loss": (today - timedelta(days=1)).isoformat(),
        "date_reported": today.isoformat(),
        "location": {"city": "Bengaluru", "state": "Karnataka", "pincode": "560068"},
        "vehicle": {"registration": "KA 05 MN 4821", "make": "Maruti Suzuki", "model": "Swift VXi", "year_of_manufacture": 2021,
                    "vehicle_class": "private_car", "idv_inr": 520000, "drivable": True},
        "driver": {"is_owner": True, "age": 34, "licence_valid": True},
        "claim_type_declared": "own_damage",
        "estimated_repair_cost_inr": 14500,
        "third_party_involved": False, "third_party_damage_inr": 0,
        "injuries_reported": False, "injured_count": 0,
        "police_report": {"filed": False},
        "contact_phone": "+919845012345",
        "claim_description": "Another car reversed into my parked car at the office basement and dented the rear bumper. I was on my way to work.",
        "claimed_damage": "Rear bumper dent and cracked tail lamp",
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return copy.deepcopy(d)


def food_delivery_fnol(**over) -> dict:
    """The section 4.1 case."""
    return base_fnol(
        claim_description="I was making a food delivery run in my own car when the accident happened. I hit a divider on the flyover and the front bumper and radiator are damaged.",
        claimed_damage="Front bumper, radiator and headlamp damage", estimated_repair_cost_inr=18000, **over)
