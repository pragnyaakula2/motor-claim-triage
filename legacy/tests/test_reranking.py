
from app.main import relevance_bonus


def test_commercial_use_boosts_hire_or_reward_clause():
    item = {
        "text": (
            "Limitations as to Use: the vehicle must not be "
            "used for hire or reward."
        )
    }

    bonus = relevance_bonus(
        item=item,
        claim_description="My private car was used for food delivery",
        claimed_damage="Vehicle body damage",
    )

    assert bonus > 0


def test_collision_boosts_accidental_damage_clause():
    item = {
        "text": (
            "Loss or damage to the vehicle caused by "
            "accidental external means."
        )
    }

    bonus = relevance_bonus(
        item=item,
        claim_description="My car was damaged in a collision",
        claimed_damage="Own damage",
    )

    assert bonus > 0


def test_unrelated_evidence_gets_no_bonus():
    item = {
        "text": "General information about the insurance company office."
    }

    bonus = relevance_bonus(
        item=item,
        claim_description="My car was damaged in a collision",
        claimed_damage="Own damage",
    )

    assert bonus == 0

