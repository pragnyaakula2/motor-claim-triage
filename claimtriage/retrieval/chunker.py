"""Clause-aware chunking (L1, section 3 of the brief).

Why not fixed-size windows: a policy defines terms in one place, grants cover in
another and restricts it in a third. Fixed windows cut across those boundaries and
lose which *part* of the policy a passage came from. Here every chunk:

* stays inside one heading-delimited section (never straddles cover / exceptions),
* carries ``section``, ``roles`` (cover/exclusion/condition/...) and a stable
  ``clause_id`` so the agent can cite and look up clauses,
* records cross-references ("Limitations as to Use", "General Exceptions", ...) so the
  agent can chase a modifying provision the claim text never mentions,
* is embedded with a breadcrumb prefix ("Policy > General Exceptions") so the section
  context is part of the vector, but cited with its raw text.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

CHUNKER_VERSION = "2"

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}

EXCL_RE = re.compile(
    r"shall not be liable|no compensation shall be payable|does not cover|not liable|shall not apply|"
    r"no indemnity shall|excludes cover|is excluded|except so far|otherwise than in accordance", re.I)
COND_RE = re.compile(r"provided (always )?that|subject to|condition precedent|only if|shall be conditions", re.I)

# (regex, canonical phrase, resolver keys) -- resolver keys are matched against chunk section names / text.
XREF_TABLE: list[tuple[re.Pattern, str, list[str]]] = [
    (re.compile(r"limitations? as to use", re.I), "Limitations as to Use", ["limitations as to use"]),
    (re.compile(r"general exceptions?", re.I), "General Exceptions", ["general exceptions"]),
    (re.compile(r"driver'?s clause", re.I), "Driver's Clause", ["driver's clause", "drivers clause"]),
    (re.compile(r"\bthe schedule\b|\bschedule hereto\b|stated in the schedule", re.I), "Schedule", ["__schedule__"]),
    (re.compile(r"geographical area", re.I), "Geographical Area", ["geographical area"]),
    (re.compile(r"terms,? exceptions,? (and )?conditions|terms,? conditions,? limitations", re.I),
     "terms, exceptions and conditions", ["general exceptions", "conditions"]),
    (re.compile(r"\bsection i\b(?!i)", re.I), "Section I", ["section i "]),
    (re.compile(r"\bsection ii\b(?!i)", re.I), "Section II", ["section ii "]),
    (re.compile(r"\bsection iii\b", re.I), "Section III", ["section iii "]),
    (re.compile(r"\bdeductible\b", re.I), "Deductible", ["deductible"]),
    (re.compile(r"\bidv\b|insured'?s declared value", re.I), "IDV", ["sum insured"]),
    (re.compile(r"hire or reward", re.I), "hire or reward", ["general exceptions"]),
]


@dataclass
class Chunk:
    clause_id: str
    doc_code: str
    source: str
    section: str
    section_key: str
    roles: list[str]
    page_start: int
    page_end: int
    text: str
    embed_text: str
    refs: list[str] = field(default_factory=list)
    content_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# text cleaning
# ---------------------------------------------------------------------------
def dedouble_line(line: str) -> str:
    """Repair web-print artefacts like 'BBooookk YYoouurr' -> 'Book Your'."""
    words = line.split()
    if not words:
        return line
    dbl = [len(w) >= 4 and len(w) % 2 == 0 and w[0::2] == w[1::2] for w in words]
    long_words = [w for w in words if len(w) >= 4]
    if long_words and sum(dbl) / max(1, len(long_words)) >= 0.6:
        return " ".join(w[0::2] if (len(w) % 2 == 0 and w[0::2] == w[1::2]) else w for w in words)
    return line


_LIGATURE_FIXES = {"bene ts": "benefits", "bene t": "benefit", "Of ce": "Office", "of cial": "official",
                   "of ces": "offices", "Of ces": "Offices"}


def clean_page(text: str, profile: dict) -> str:
    for pat in profile.get("strip_block_patterns", []):
        text = re.sub(pat, "", text, flags=re.S)
    text = text.replace("\x00", "fi")
    for bad, good in _LIGATURE_FIXES.items():
        text = text.replace(bad, good)
    lines = []
    for ln in text.splitlines():
        if re.search(r"[\ue000-\uf8ff]", ln):  # icon-font glyphs = navigation menus on the web print
            continue
        if any(re.search(p, ln) for p in profile.get("strip_line_patterns", [])):
            continue
        if profile.get("dedouble"):
            ln = dedouble_line(ln)
        lines.append(ln.rstrip())
    return "\n".join(lines)


def _paragraphs(text: str) -> list[str]:
    paras = re.split(r"\n\s*\n", text)
    out = []
    for p in paras:
        p = re.sub(r"\s*\n\s*", " ", p)
        p = re.sub(r"[ \t]{2,}", " ", p).strip()
        if p:
            out.append(p)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# heading detection
# ---------------------------------------------------------------------------
def detect_heading(para: str) -> tuple[str, str, list[str]] | None:
    """Return (section name, section_key, roles) if the paragraph opens a new section."""
    first = para.split("  ")[0].strip()
    head = para[:140]
    m = re.match(r"^SECTION\s+(I{1,3}|IV)\s*[:\-–]\s*(.*)", head)
    if m:
        num = m.group(1)
        title = re.split(r"\s{2,}|The Company", m.group(2))[0].strip().title()
        return (f"Section {num} - {title}", f"section {num.lower()} ", ["cover"])
    if re.match(r"^GENERAL EXCEPTIONS", head):
        return ("General Exceptions (applicable to all sections)", "general exceptions", ["exclusion"])
    if re.match(r"^DEDUCTIBLE\b", head) and len(para) < 400:
        return ("Deductible", "deductible", ["condition"])
    if re.match(r"^CONDITIONS\b", head):
        return ("Conditions", "conditions", ["condition"])
    if re.match(r"^SUM INSURED", head):
        return ("Sum Insured - Insured's Declared Value (IDV)", "sum insured", ["definition"])
    if re.match(r"^THE SCHEDULE OF DEPRECIATION", head):
        return ("Schedule of depreciation for fixing IDV", "sum insured", ["definition"])
    if re.match(r"^AVOIDANCE OF CERTAIN TERMS", head):
        return ("Section II - Avoidance of certain terms and right of recovery", "section ii ", ["cover", "condition"])
    if re.match(r"^APPLICATION OF LIMITS OF INDEMNITY", head):
        return ("Section II - Application of limits of indemnity", "section ii ", ["cover", "condition"])
    m = re.match(r"^IMT\.?\s*(\d+[A-C]?(?:\([A-C]\))?)\.?\s*(.*)", head)
    if m:
        title = re.sub(r"\s+", " ", m.group(2)).strip().title()[:80]
        return (f"Endorsement IMT.{m.group(1)} - {title}".rstrip(" -"), f"imt.{m.group(1).lower()}", ["endorsement"])
    if re.match(r"^ENDORSEMENT WORDINGS", head):
        return ("Endorsement wordings", "endorsements", ["endorsement"])
    if re.match(r"^Grievance Re[a-z]+", head):
        return ("Grievance redressal and Ombudsman", "grievance", ["admin"])
    if re.match(r"^STANDARD FORM FOR PRIVATE CAR", head):
        return ("Preamble", "preamble", ["definition"])
    return None


def _split_long(p: str, limit: int) -> list[str]:
    if len(p) <= limit:
        return [p]
    sents = re.split(r"(?<=[.;])\s+", p)
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + len(s) + 1 > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def extract_refs(text: str) -> list[str]:
    found = []
    for rx, phrase, _ in XREF_TABLE:
        if rx.search(text) and phrase not in found:
            found.append(phrase)
    return found


def chunk_document(pages: list[tuple[int, str]], source: str, profile: dict, target: int = 900) -> list[Chunk]:
    doc_code = profile["doc_code"]
    kind = profile["kind"]
    skip = set(profile.get("skip_pages", []))

    # 1. paragraphs with page numbers, merging paragraphs split by a page break
    paras: list[dict] = []
    for pno, raw in pages:
        if pno in skip:
            continue
        for p in _paragraphs(clean_page(raw, profile)):
            if paras and paras[-1]["page_end"] == pno - 1 and not re.search(r"[.;:]$", paras[-1]["text"]) \
                    and p[:1].islower() and not detect_heading(p):
                paras[-1]["text"] += " " + p
                paras[-1]["page_end"] = pno
            else:
                paras.append({"text": p, "page_start": pno, "page_end": pno})

    if kind == "procedure":  # drop navigation/marketing fragments from the web page
        paras = [p for p in paras if re.search(r"[.:;?]", p["text"]) and len(p["text"].split()) >= 12]

    # 2. group into sections
    sections: list[dict] = []
    cur = {"name": profile["title"], "key": "document", "roles": ["procedure"] if kind == "procedure" else ["definition"], "paras": []}
    for p in paras:
        h = detect_heading(p["text"]) if kind == "policy" else None
        if h:
            if cur["paras"]:
                sections.append(cur)
            cur = {"name": h[0], "key": h[1], "roles": h[2], "paras": []}
        cur["paras"].append(p)
    if cur["paras"]:
        sections.append(cur)

    # 3. pack paragraphs into chunks inside each section
    chunks: list[Chunk] = []
    counters: dict[str, int] = {}
    for sec in sections:
        slug = re.sub(r"[^A-Z0-9]+", "-", sec["name"].upper().split(" - ")[0]).strip("-")[:18] or "DOC"
        if sec["key"].startswith("imt."):
            slug = "IMT-" + sec["key"].split(".", 1)[1].upper()
        elif sec["key"].startswith("section "):
            slug = "S" + sec["key"].split()[1].upper()
        elif sec["key"] == "general exceptions":
            slug = "GE"
        buf, ps, pe = "", None, None

        def flush():
            nonlocal buf, ps, pe
            if not buf.strip():
                return
            n = counters.get(f"{doc_code}:{slug}", 0) + 1
            counters[f"{doc_code}:{slug}"] = n
            text = _norm(buf)
            roles = list(sec["roles"])
            if EXCL_RE.search(text) and "exclusion" not in roles:
                roles.append("exclusion")
            if COND_RE.search(text) and "condition" not in roles and "cover" in roles:
                roles.append("condition")
            crumb = f"{profile['title']} > {sec['name']}"
            chunks.append(Chunk(
                clause_id=f"{doc_code}:{slug}:{n:02d}", doc_code=doc_code, source=source,
                section=sec["name"], section_key=sec["key"], roles=roles,
                page_start=ps, page_end=pe, text=text, embed_text=f"{crumb}\n{text}",
                refs=extract_refs(text),
                content_hash=hashlib.sha256(text.encode()).hexdigest()[:16]))
            buf, ps, pe = "", None, None

        for p in sec["paras"]:
            for piece in _split_long(p["text"], target):
                if buf and len(buf) + len(piece) + 1 > target:
                    flush()
                if not buf:
                    ps = p["page_start"]
                buf = f"{buf} {piece}".strip()
                pe = p["page_end"]
        flush()
    return chunks
