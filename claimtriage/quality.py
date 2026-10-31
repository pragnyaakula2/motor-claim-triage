"""L6: score a sample of live runs automatically and chart quality over time (reference-free metrics)."""
from __future__ import annotations

import time

from . import metrics as M
from .evalmetrics import claim_facts_text, score_answer


def score_live_run(store, run: dict, out: dict, tools) -> dict:
    claim = run["claim"]
    text = f'{claim["claim_description"]} {claim["claimed_damage"]} {claim_facts_text(claim)}'
    s = score_answer(out["answer"], tools.retrieved, text, run["handoff"])
    verdict = out.get("verification") or {}
    point = {"ts": time.time(), "run_id": run["run_id"], "groundedness": s["groundedness"], "faithfulness": s["faithfulness"],
             "hallucination": int(s["hallucination"]), "verified_first_try": int(out.get("attempts", 1) == 1),
             "verification_passed": int(bool(verdict.get("passed")))}
    store.add_quality(point)
    pts = store.quality_points()[-50:]
    for m in ("groundedness", "faithfulness", "hallucination", "verified_first_try"):
        M.QUALITY.labels(m).set(sum(p[m] for p in pts) / len(pts))
    return point
