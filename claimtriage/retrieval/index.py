"""Hybrid (dense + BM25) policy index on LangChain's FAISS store (L1)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
import faiss

from .bm25 import BM25, tokenize
from .chunker import XREF_TABLE
from .embedder import Embedder, LangChainEmbeddings


class PolicyIndex:
    def __init__(self, store_dir: Path, embedder: Embedder):
        self.dir = Path(store_dir)
        self.embedder = embedder
        manifest = json.loads((self.dir / "manifest.json").read_text())
        self.manifest = manifest
        if manifest["embedding_model_id"] != embedder.model_id:
            raise RuntimeError(
                f"Index was built with {manifest['embedding_model_id']} but the service is configured with "
                f"{embedder.model_id}. Re-run the pipeline (python -m claimtriage.pipeline.build).")
        self.chunks: list[dict] = json.loads((self.dir / "chunks.json").read_text())
        self.by_id = {c["clause_id"]: c for c in self.chunks}
        self.vs = FAISS.load_local(str(self.dir / "faiss"), LangChainEmbeddings(embedder), "policy",
                                   allow_dangerous_deserialization=True)
        self.bm25 = BM25([c["text"] for c in self.chunks])
        self.version = manifest["index_version"]

    # ------------------------------------------------------------------
    def _embed_query(self, query: str) -> np.ndarray:
        """Query-embedding cache (D7): identical sub-queries recur across runs."""
        from collections import OrderedDict
        from .. import metrics as M
        if not hasattr(self, "_qcache"):
            self._qcache: OrderedDict = OrderedDict()
        hit = self._qcache.get(query)
        if hit is not None:
            self._qcache.move_to_end(query)
            M.CACHE.labels("embedding", "hit").inc()
            return hit
        M.CACHE.labels("embedding", "miss").inc()
        v = self.embedder.encode([query]).astype("float32")
        self._qcache[query] = v
        if len(self._qcache) > 4096:
            self._qcache.popitem(last=False)
        return v

    def _dense_scores(self, query: str) -> np.ndarray:
        q = self._embed_query(query)
        scores, idx = self.vs.index.search(q, len(self.chunks))
        out = np.zeros(len(self.chunks), dtype=np.float32)
        for s, i in zip(scores[0], idx[0]):
            if i >= 0:
                out[int(i)] = s
        return out

    def search(self, query: str, top_k: int = 5, section_type: str | None = None, mode: str = "hybrid") -> list[dict]:
        n = len(self.chunks)
        dense = self._dense_scores(query) if mode in ("hybrid", "dense") else np.zeros(n)
        bm = np.array(self.bm25.scores(query)) if mode in ("hybrid", "keyword") else np.zeros(n)
        cov = np.array(self.bm25.coverage(query))
        allowed = [i for i, c in enumerate(self.chunks) if section_type is None or section_type in c["roles"]]
        if not allowed:
            return []

        def ranks(vals: np.ndarray) -> dict[int, int]:
            order = sorted(allowed, key=lambda i: -vals[i])
            return {i: r for r, i in enumerate(order)}

        rd, rb = ranks(dense), ranks(bm)
        fused = {i: (1 / (60 + rd[i]) if mode != "keyword" else 0) + (1 / (60 + rb[i]) if mode != "dense" else 0)
                 for i in allowed}
        order = sorted(allowed, key=lambda i: -fused[i])[:top_k]
        res = []
        for i in order:
            c = self.chunks[i]
            d = float(max(dense[i], 0.0)) if mode != "keyword" else float(cov[i])
            score = round(0.5 * d + 0.5 * float(cov[i]), 4) if mode != "keyword" else round(float(cov[i]), 4)
            res.append({**{k: c[k] for k in ("clause_id", "source", "page_start", "page_end", "section", "text", "refs")},
                        "section_type": ",".join(c["roles"]), "score": score})
        return res

    def get_clause(self, clause_id: str) -> dict | None:
        c = self.by_id.get(clause_id)
        if not c:
            return None
        return {**{k: c[k] for k in ("clause_id", "source", "page_start", "page_end", "section", "text", "refs")},
                "section_type": ",".join(c["roles"]), "score": 1.0}

    def neighbours(self, clause_id: str) -> list[str]:
        c = self.by_id.get(clause_id)
        return [x["clause_id"] for x in self.chunks if c and x["section_key"] == c["section_key"] and x["doc_code"] == c["doc_code"]]

    def cross_refs(self, clause_id: str) -> dict | None:
        c = self.by_id.get(clause_id)
        if c is None:
            return None
        refs, queries = [], []
        for rx, phrase, keys in XREF_TABLE:
            if not rx.search(c["text"]):
                continue
            if keys == ["__schedule__"]:
                refs.append({"phrase": phrase, "resolved_to": [], "status": "unresolved",
                             "note": "Refers to the policy Schedule, which is not part of the indexed corpus."})
                continue
            targets = []
            for x in self.chunks:
                if x["clause_id"] == clause_id:
                    continue
                hay_name = (x["section"] + " ").lower()
                hay_key = x["section_key"]
                if any(k in hay_name or k == hay_key or k in hay_key for k in keys):
                    targets.append(x["clause_id"])
                elif any(re.search(r"(?i)^\W*" + re.escape(k), x["text"][:80]) for k in keys if len(k) > 8):
                    targets.append(x["clause_id"])
            if targets:
                refs.append({"phrase": phrase, "resolved_to": targets[:6], "status": "resolved", "note": ""})
                queries.append(f"{phrase} restriction exclusion condition")
            else:
                refs.append({"phrase": phrase, "resolved_to": [], "status": "unresolved",
                             "note": f"'{phrase}' is referred to but no section defining it exists in the indexed corpus."})
                queries.append(f"{phrase} definition scope")
        if "cover" in c["roles"] and any(x["section_key"] == "preamble" for x in self.chunks):
            # The preamble makes every grant of cover "subject to the terms exceptions and conditions"
            # of the policy, so a cover clause implicitly depends on the policy-wide restrictions.
            implicit = [x["clause_id"] for x in self.chunks
                        if x["section_key"] in ("general exceptions", "conditions", "deductible")]
            refs.append({"phrase": "policy-wide exceptions and conditions (implicit via Preamble)",
                         "resolved_to": implicit[:12], "status": "resolved",
                         "note": "Preamble: cover is 'subject to the terms exceptions and conditions contained herein'."})
            queries.append("general exceptions use of the vehicle restriction")
        return {"clause_id": clause_id, "refs": refs, "suggested_queries": queries}
