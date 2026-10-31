"""L4 guardrails: prompt-injection defence, toxicity detection, output authority filter.

Honest scope: these are layered heuristics, not a proof of safety.
  * injection  - weighted pattern scoring on every free-text field BEFORE it can reach a model;
                 high score => blocked at the ingestion boundary (quarantined, no run starts);
                 medium => neutralised + flagged, and the run is forced to human review.
  * containment - agents hold read-only tools only; tool arguments are validated (agents/tools.py);
                 free text only ever enters prompts as an escaped JSON string inside delimiters.
  * output     - the system can never emit an approval/rejection/payment statement; such text is
                 removed and the run is flagged.
  * toxicity   - lexicon-based (no model download). It flags, it does not reject abusive customers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import metrics as M

INJECTION_PATTERNS: list[tuple[str, float, re.Pattern]] = [
    ("override_instructions", 0.9, re.compile(r"(ignore|disregard|forget|override|bypass)\W+(all\W+|any\W+|the\W+|your\W+|previous\W+|prior\W+|above\W+|earlier\W+){1,4}(instructions?|prompts?|rules?|guidelines?|constraints?|policies|checks?)", re.I)),
    ("role_marker", 0.85, re.compile(r"(<\|?\s*(im_start|im_end|system|assistant)\s*\|?>|\[/?(INST|SYS)\]|^\s*(system|assistant)\s*:|#{2,}\s*system)", re.I | re.M)),
    ("json_action_injection", 0.9, re.compile(r"\"\s*action\s*\"\s*:\s*\"\s*(final|tool)\s*\"", re.I)),
    ("force_decision", 0.8, re.compile(r"(approve|accept|settle|pass)\W+(this|the|my)\W+(claim|request)|mark\W+(this\W+|the\W+)?(claim\W+)?(as\W+)?(approved|covered|valid|fast[\W_]?track)|(set|change|force)\W+(the\W+)?(route|position|coverage|status)\W+(to|as)|route\W+(this\W+)?(claim\W+)?to\W+fast[\W_]?track", re.I)),
    ("reveal_prompt", 0.8, re.compile(r"(reveal|show|print|repeat|output|leak)\W+(me\W+)?(your\W+|the\W+)?(system\W+|hidden\W+|initial\W+)?(prompt|instructions?|api[\W_]?key|secrets?|password|environment)", re.I)),
    ("persona_switch", 0.5, re.compile(r"(you are now|from now on you|act as|pretend (to be|you are)|new instructions?:)", re.I)),
    ("skip_verification", 0.6, re.compile(r"(do not|don't|never|skip|without)\W+(verify|verification|cite|citations?|escalate|search|check)", re.I)),
    ("tool_abuse", 0.5, re.compile(r"(call|invoke|use|run|execute)\W+(the\W+)?(tool|function)|search_policy|get_clause|list_cross_references|get_claim_fields", re.I)),
    ("encoded_blob", 0.4, re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")),
]

TOXIC_TERMS = [
    "idiot", "stupid", "moron", "bastard", "bloody fool", "scumbag", "useless people", "damn you",
    "shut up", "fuck", "shit", "bitch", "asshole", "bakwas", "harami", "chutiya", "madarchod", "bhenchod",
    "i will kill", "i will burn", "burn your office", "threaten",
]
TOXIC_RE = re.compile("|".join(re.escape(t) for t in TOXIC_TERMS), re.I)

DETERMINATION_PATTERNS = [
    re.compile(r"\b(claim|request)\b[^.]{0,30}\b(is|has been|have been|will be|stands?|being)\b[^.]{0,12}\b(approved|rejected|denied|declined|accepted|settled|paid|repudiated)\b", re.I),
    re.compile(r"\bwe\b[^.]{0,12}\b(approve|reject|deny|decline|repudiate|will pay|will reimburse|will settle)\b", re.I),
    re.compile(r"\b(hereby|is|are)\s+(approved|rejected|denied|repudiated)\b", re.I),
    re.compile(r"\b(final|binding)\s+(decision|determination|settlement)\b", re.I),
    re.compile(r"\b(guarantee[sd]?|assured payout|you will receive|will be paid)\b", re.I),
    re.compile(r"(settlement|payout|payable|compensation)\s+(amount|of)\s*[:\-]?\s*(rs\.?|inr|₹)\s?\d", re.I),
]
AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s?([\d,]+(?:\.\d+)?)", re.I)

ALLOWED_POSITIONS = {
    "PRIMA_FACIE_WITHIN_COVER", "RESTRICTION_LIKELY_APPLIES", "RESTRICTION_REQUIRES_VERIFICATION",
    "CONDITION_NOT_MET_RISK", "INSUFFICIENT_EVIDENCE", "OUT_OF_CORPUS",
}


@dataclass
class ScanResult:
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    action: str = "allow"  # allow | flag | block
    sanitized: str = ""


def sanitize_text(text: str) -> str:
    """Make free text safe to embed as an escaped JSON string inside prompt delimiters."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f​-‏‪-‮⁦-⁩]", " ", text)
    return text.replace("<", "＜").replace(">", "＞")


def scan_injection(text: str, block_at: float = 0.8, flag_at: float = 0.4) -> ScanResult:
    keep = 1.0
    flags = []
    clean = text
    for name, w, rx in INJECTION_PATTERNS:
        if rx.search(text):
            flags.append(name)
            keep *= (1 - w)
            clean = rx.sub("[removed: suspected instruction]", clean)
    score = round(1 - keep, 3)
    action = "block" if score >= block_at else "flag" if score >= flag_at else "allow"
    return ScanResult(score=score, flags=flags, action=action, sanitized=sanitize_text(clean))


def scan_toxicity(text: str) -> ScanResult:
    hits = {m.group(0).lower() for m in TOXIC_RE.finditer(text)}
    score = min(1.0, round(0.4 * len(hits), 2))
    return ScanResult(score=score, flags=sorted(hits), action="flag" if hits else "allow", sanitized=text)


def scan_inputs(fields: dict[str, str], block_at: float, flag_at: float) -> dict:
    """Scan every free-text field. Returns {'action','flags','sanitized':{field:text},'detail':{...}}."""
    worst = "allow"
    flags: list[str] = []
    detail: dict = {}
    sanitized: dict = {}
    for name, text in fields.items():
        inj = scan_injection(text, block_at, flag_at)
        tox = scan_toxicity(text)
        sanitized[name] = inj.sanitized
        detail[name] = {"injection_score": inj.score, "injection_flags": inj.flags, "toxicity": tox.flags}
        if inj.action == "block":
            worst = "block"
        elif inj.action == "flag" and worst != "block":
            worst = "flag"
        flags += [f"injection:{f}" for f in inj.flags]
        flags += [f"toxicity:{f}" for f in tox.flags]
        M.GUARD.labels("injection", inj.action).inc()
        if tox.flags:
            M.GUARD.labels("toxicity_input", "flag").inc()
    return {"action": worst, "flags": sorted(set(flags)), "sanitized": sanitized, "detail": detail}


def filter_output(answer: dict, allowed_amounts: set[float] | None = None) -> tuple[dict, list[str]]:
    """Remove determinations/payout statements the system is not authorised to make.

    Returns (clean answer, violations). Position outside the allowed enum is a violation the
    caller must turn into an escalation.
    """
    violations: list[str] = []
    allowed_amounts = allowed_amounts or set()
    out = dict(answer)

    def scrub(s: str, where: str) -> str:
        if not isinstance(s, str):
            return s
        for rx in DETERMINATION_PATTERNS:
            if rx.search(s):
                violations.append(f"determination_language:{where}")
                s = rx.sub("[removed by output filter: determinations are made by a human claims handler]", s)
        for m in AMOUNT_RE.finditer(s):
            val = float(m.group(1).replace(",", "") or 0)
            if val not in allowed_amounts and val > 0:
                violations.append(f"unauthorised_amount:{where}")
                s = s.replace(m.group(0), "[amount removed]")
        tox = scan_toxicity(s)
        if tox.flags:
            violations.append(f"toxic_output:{where}")
            s = TOXIC_RE.sub("[removed]", s)
        return s

    for key in ("summary", "route_reason", "escalation_reason"):
        if key in out:
            out[key] = scrub(out[key], key)
    if isinstance(out.get("restriction_assessment"), list):
        out["restriction_assessment"] = [
            {**r, "reason": scrub(r.get("reason", ""), "restriction_assessment")} if isinstance(r, dict) else r
            for r in out["restriction_assessment"]]
    if out.get("position") not in ALLOWED_POSITIONS:
        violations.append(f"position_not_allowed:{out.get('position')}")
    for v in violations:
        M.GUARD.labels("output_filter", "removed").inc()
    return out, violations
