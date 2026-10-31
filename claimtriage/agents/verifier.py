"""A3 verification: an answer is returned only if it is supported by what was retrieved in THIS run.

Deterministic checks (always run):
  V1 every citation's clause_id resolves to a passage retrieved in this run
  V2 every citation quote is a verbatim (normalised) substring of that passage
  V3 amounts / percentages / durations in the summary appear in a cited passage or in the claim
  V4 cover positions: the modifying provisions were actually looked for (cross-references or exclusion/condition search)
  V5 a cover citation exists for cover-type positions; restriction positions cite a restriction passage
  V6 claim facts that trigger a use restriction (paid delivery / hire) must be addressed by a cited restriction passage
  V7 retrieval strength: the best retrieved score clears the weak-retrieval threshold
Optional (VERIFIER_LLM_CHECK): an LLM entailment check per citation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .common import norm_text

USE_RESTRICTION_RE = re.compile(r"otherwise than in accordance with|limitations? as to use|hire or reward|use for (?:organised )?racing", re.I)
COVER_POSITIONS = {"PRIMA_FACIE_WITHIN_COVER", "RESTRICTION_LIKELY_APPLIES", "RESTRICTION_REQUIRES_VERIFICATION", "CONDITION_NOT_MET_RISK"}
RESTRICTION_POSITIONS = {"RESTRICTION_LIKELY_APPLIES", "RESTRICTION_REQUIRES_VERIFICATION", "CONDITION_NOT_MET_RISK"}
AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s?([\d,]+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s?%|(\d+)\s?(?:days?|months?)", re.I)


def is_restrictive(roles: set[str]) -> bool:
    """An exclusion, or a pure condition. A cover clause that merely says 'subject to' is not itself a restriction."""
    return "exclusion" in roles or ("condition" in roles and "cover" not in roles)


@dataclass
class Check:
    id: str
    passed: bool
    detail: str = ""

    def to_dict(self):
        return {"id": self.id, "passed": self.passed, "detail": self.detail}


@dataclass
class Verdict:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    repaired_quotes: int = 0

    def to_dict(self):
        return {"passed": self.passed, "checks": [c.to_dict() for c in self.checks], "issues": self.issues,
                "codes": self.codes}


def _find_quote(norm_quote: str, norm_passage: str) -> bool:
    return len(norm_quote) >= 15 and norm_quote in norm_passage


def _canon(g: str) -> str:
    f = float(g.replace(",", ""))
    return str(int(f)) if f == int(f) else str(f)


def _nums(text: str) -> set[str]:
    out = set()
    for m in AMOUNT_RE.finditer(text):
        g = next((x for x in m.groups() if x), None)
        if g:
            try:
                out.add(_canon(g))
            except ValueError:
                pass
    return out


def verify_answer(answer: dict, retrieved: dict[str, dict], *, handoff: dict, claim: dict,
                  modifier_searched: bool, best_score: float, weak_threshold: float) -> Verdict:
    v = Verdict(passed=True)

    def add(cid: str, ok: bool, detail: str = "", code: str | None = None):
        v.checks.append(Check(cid, ok, detail))
        if not ok:
            v.passed = False
            v.issues.append(f"{cid}: {detail}")
            if code and code not in v.codes:
                v.codes.append(code)

    position = answer.get("position")
    cits = answer.get("citations") or []
    if not isinstance(cits, list):
        cits = []
    escalating = bool(answer.get("needs_escalation")) or position in ("INSUFFICIENT_EVIDENCE", "OUT_OF_CORPUS")

    # V1 + V2 -------------------------------------------------------------------------------
    bad_ids, bad_quotes = [], []
    for c in cits:
        cid = c.get("clause_id") if isinstance(c, dict) else None
        p = retrieved.get(cid)
        if p is None:
            bad_ids.append(str(cid))
            continue
        if norm_text(c.get("quote", "")) and not _find_quote(norm_text(c.get("quote", "")), norm_text(p["text"])):
            bad_quotes.append(cid)
    add("V1_citations_resolve", not bad_ids,
        f"citation clause_id(s) not retrieved in this run: {bad_ids}" if bad_ids else f"{len(cits)} citation(s) resolve", "UNRESOLVED_CITATION")
    add("V2_quotes_verbatim", not bad_quotes,
        f"quote not found verbatim in retrieved text for: {bad_quotes}" if bad_quotes else "all quotes found in retrieved text", "UNSUPPORTED_QUOTE")

    if escalating:
        # an escalation needs no cover evidence, but any citation it makes must still be real (V1/V2 above)
        add("V7_retrieval_strength", True, "escalating answer; strength check not required")
        return v

    # V3 ---------------------------------------------------------------------------------------
    cited_text = " ".join(retrieved[c["clause_id"]]["text"] for c in cits if isinstance(c, dict) and c.get("clause_id") in retrieved)
    claim_text = " ".join(str(x) for x in (claim.get("claim_description"), claim.get("claimed_damage"),
                                           claim.get("estimated_repair_cost_inr"), claim.get("vehicle", {}).get("idv_inr")))
    allowed = _nums(cited_text) | _nums(claim_text) | {_canon(str(claim.get("estimated_repair_cost_inr") or 0))}
    unsupported = sorted(n for n in _nums(answer.get("summary", "")) if n not in allowed)
    add("V3_figures_supported", not unsupported,
        f"figures in summary not found in cited text or claim: {unsupported}" if unsupported else "no unsupported figures", "UNSUPPORTED_FIGURE")

    # V4 -----------------------------------------------------------------------------------------
    if position in COVER_POSITIONS:
        add("V4_modifiers_examined", modifier_searched,
            "answer grants cover but no cross-reference lookup or exclusion/condition search was performed" if not modifier_searched
            else "modifying provisions were searched for", "MODIFIERS_NOT_EXAMINED")

    # V5 -----------------------------------------------------------------------------------------
    def roles_of(c):
        return set((retrieved.get(c.get("clause_id"), {}).get("section_type") or "").split(","))

    if position in COVER_POSITIONS:
        has_cover = any("cover" in roles_of(c) for c in cits if isinstance(c, dict))
        add("V5a_cover_cited", has_cover, "no cited passage is a cover provision" if not has_cover else "cover provision cited", "NO_COVER_CITATION")
    if position in RESTRICTION_POSITIONS:
        has_restr = any(is_restrictive(roles_of(c)) for c in cits if isinstance(c, dict))
        add("V5b_restriction_cited", has_restr, "restriction position without a cited exclusion/condition passage" if not has_restr
            else "restriction passage cited", "NO_RESTRICTION_CITATION")

    # V6 -----------------------------------------------------------------------------------------
    use = (handoff.get("entities") or {}).get("use_purpose", "unknown")
    if use in ("commercial_delivery", "hire_or_reward") and position in COVER_POSITIONS:
        cited_use = [c for c in cits if isinstance(c, dict) and c.get("clause_id") in retrieved
                     and USE_RESTRICTION_RE.search(retrieved[c["clause_id"]]["text"])]
        assessed = any(isinstance(r, dict) and r.get("clause_id") in {c["clause_id"] for c in cited_use}
                       for r in (answer.get("restriction_assessment") or []))
        add("V6_use_restriction_addressed", bool(cited_use) and assessed,
            "claim facts indicate paid/commercial use but no cited passage states a use restriction (or it is not assessed)"
            if not (cited_use and assessed) else "use restriction cited and assessed", "USE_RESTRICTION_NOT_ADDRESSED")
        if answer.get("within_permitted_use") not in ("yes", "no", "unverifiable"):
            add("V6b_permitted_use_stated", False, "within_permitted_use must be yes/no/unverifiable when use is commercial", "USE_NOT_STATED")

    # V7 -----------------------------------------------------------------------------------------
    add("V7_retrieval_strength", best_score >= weak_threshold,
        f"best retrieval score {best_score:.2f} below threshold {weak_threshold:.2f}" if best_score < weak_threshold
        else f"best score {best_score:.2f}", "WEAK_RETRIEVAL")
    return v
