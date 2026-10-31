from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")
STOP = set("the of a an and or to in is are be by for on at as with that this it from any shall which such not was were has have had will would can may".split())


def tokenize(text: str) -> list[str]:
    toks = [t for t in _TOKEN.findall(text.lower()) if t not in STOP]
    return [t[:-1] if len(t) > 4 and t.endswith("s") else t for t in toks]  # crude plural stem


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.toks = [tokenize(d) for d in docs]
        self.tf = [Counter(t) for t in self.toks]
        self.len = [len(t) for t in self.toks]
        self.avg = sum(self.len) / max(1, len(self.len))
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.max_idf = max(self.idf.values()) if self.idf else 1.0

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for w in q:
                f = tf.get(w, 0)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out

    def coverage(self, query: str) -> list[float]:
        """Absolute idf-weighted fraction of query terms present in each doc (0..1).

        Query terms absent from the whole corpus count at max idf, so off-topic
        queries score low everywhere - this is the signal behind weak-retrieval escalation (F7).
        """
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return [0.0] * len(self.tf)
        w = {t: self.idf.get(t, self.max_idf) for t in q}
        total = sum(w.values())
        return [sum(w[t] for t in q if t in tf) / total for tf in self.tf]
