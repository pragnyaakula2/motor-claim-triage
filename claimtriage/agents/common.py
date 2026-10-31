from __future__ import annotations

import json
import re


def parse_json_object(text: str) -> dict:
    """Tolerant JSON extraction: strips code fences and surrounding prose; raises ValueError if none."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        v = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("no JSON object found")
        v = json.loads(t[i:j + 1])
    if not isinstance(v, dict):
        raise ValueError("top-level JSON is not an object")
    return v


def norm_text(s: str) -> str:
    """Normalise for verbatim-quote checks: case, quotes/dashes, punctuation and whitespace."""
    s = s.lower().replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.replace("„", "'").replace("‟", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()
