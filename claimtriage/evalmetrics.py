"""Evaluation metrics (L3) - deterministic, reference-based where a gold passage exists, reference-free otherwise.

Definitions (all in [0,1]; stated in docs/EVALUATION.md):
  context_relevance    precision of retrieval: share of the unique passages the agent retrieved that contain a gold
                       anchor phrase (the text of the passage that supports the expected outcome).
  context_recall       share of gold anchors present in the retrieved set.
  groundedness         share of answer sentences (summary + restriction reasons) whose content words are supported
                       (>= 0.6 overlap) by the retrieved passages, the claim text or the triage hand-off.
  faithfulness         share of attribution claims that are correct: each citation's quote must be verbatim in the cited
                       passage, each section/page named in the prose must belong to a cited clause, and the position must be
                       consistent with the roles of the cited passages.
  hallucination_rate   share of runs whose output contains >=1 fabricated element: unresolved clause id, non-verbatim
                       quote, unsupported figure, or an ungrounded sentence.
These are lexical proxies (no model judge), chosen so they are reproducible offline and free. Where an LLM judge is wanted,
plug it into `groundedness_sentence` (see docs/EVALUATION.md); the harness reports the proxy and says so.
"""
from __future__ import annotations

import re

from .agents.common import norm_text
from .agents.verifier import RESTRICTION_POSITIONS, _nums, is_restrictive

STOP = set("""about above after again against being below between both could does doing down during each from further have having
here itself just more most other over same should some such than that their them then there these they this those through under until
very were what when where which while will with would your into onto upon also within without shall""".split())


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4 and w not in STOP}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if len(s.strip()) > 12]


def groundedness_sentence(sentence: str, context_words: set[str], threshold: float = 0.6) -> bool:
    w = content_words(sentence)
    return not w or len(w & context_words) / len(w) >= threshold


def answer_sentences(answer: dict) -> list[str]:
    out = sentences(answer.get("summary", ""))
    for r in answer.get("restriction_assessment") or []:
        if isinstance(r, dict):
            out += sentences(r.get("reason", ""))
    return out


def claim_facts_text(claim: dict) -> str:
    """Structured claim fields rendered as words, so sentences about them (e.g. 'no police report is recorded') are grounded."""
    pr = claim.get("police_report", {})
    return (f'police report {"filed FIR " + str(pr.get("fir_number")) if pr.get("filed") else "not filed none recorded"} '
            f'vehicle {"drivable" if claim.get("vehicle", {}).get("drivable") else "not drivable towed"} '
            f'driver {"owner" if claim.get("driver", {}).get("is_owner") else "not owner"} licence {claim.get("driver", {}).get("licence_valid")} '
            f'declared {claim.get("claim_type_declared")} third party {claim.get("third_party_involved")} injuries {claim.get("injuries_reported")}')


def score_answer(answer: dict, retrieved: dict[str, dict], claim_text: str, handoff: dict) -> dict:
    ctx = set()
    for p in retrieved.values():
        ctx |= content_words(p["text"]) | content_words(p["section"])
    ctx |= content_words(claim_text) | content_words(str(handoff.get("entities", "")))
    ctx |= {"section", "claim", "claims", "policy", "wording", "corpus", "indexed", "cover", "covers", "page", "clause",
            "provision", "provisions", "retrieved", "described", "describes", "confirmed", "confirm", "text", "schedule", "potential"}
    sents = answer_sentences(answer)
    ungrounded = [s for s in sents if not groundedness_sentence(s, ctx)]
    groundedness = 1.0 if not sents else 1 - len(ungrounded) / len(sents)

    cits = [c for c in (answer.get("citations") or []) if isinstance(c, dict)]
    checks = ok = 0
    unresolved = unverbatim = 0
    for c in cits:
        p = retrieved.get(c.get("clause_id"))
        checks += 1
        if p is None:
            unresolved += 1
            continue
        if len(norm_text(c.get("quote", ""))) >= 15 and norm_text(c["quote"]) in norm_text(p["text"]):
            ok += 1
        else:
            unverbatim += 1
    cited_sections = " ".join(retrieved[c["clause_id"]]["section"].lower() for c in cits if c.get("clause_id") in retrieved)
    for s in sents:
        for m in re.finditer(r"(general exceptions?|section i{1,3}\b|conditions?\b|deductible|imt\.\d+)", s, re.I):
            checks += 1
            name = m.group(1).lower().rstrip("s") if m.group(1).lower().startswith("general") else m.group(1).lower()
            if name.rstrip("s") in cited_sections or name in cited_sections:
                ok += 1
    pos = answer.get("position")
    if pos in RESTRICTION_POSITIONS:
        checks += 1
        roles = [set((retrieved.get(c.get("clause_id"), {}).get("section_type") or "").split(",")) for c in cits]
        ok += int(any(is_restrictive(r) for r in roles))
    faithfulness = 1.0 if checks == 0 else ok / checks

    cited_text = " ".join(retrieved[c["clause_id"]]["text"] for c in cits if c.get("clause_id") in retrieved)
    unsupported_fig = sorted(n for n in _nums(answer.get("summary", "")) if n not in _nums(cited_text) | _nums(claim_text))
    halluc = bool(unresolved or unverbatim or unsupported_fig or ungrounded)
    return {"groundedness": round(groundedness, 4), "faithfulness": round(faithfulness, 4), "hallucination": halluc,
            "unresolved_citations": unresolved, "unverbatim_quotes": unverbatim, "unsupported_figures": unsupported_fig,
            "ungrounded_sentences": ungrounded[:3]}


def score_retrieval(retrieved: dict[str, dict], anchors: list[str]) -> dict:
    if not anchors:
        return {"context_relevance": None, "context_recall": None}
    na = [norm_text(a) for a in anchors]
    norm_pass = {cid: norm_text(p["text"]) for cid, p in retrieved.items()}
    relevant = [cid for cid, t in norm_pass.items() if any(a in t for a in na)]
    found = [a for a in na if any(a in t for t in norm_pass.values())]
    return {"context_relevance": round(len(relevant) / max(1, len(retrieved)), 4),
            "context_recall": round(len(found) / len(na), 4)}
