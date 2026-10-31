"""Embedding backends behind one interface.

``sentence_transformers`` is the production backend (runs locally, no API cost).
``hashing`` is a dependency-free, deterministic lexical embedder used for CI,
offline development and the load-test environment; it is NOT a semantic model and
its retrieval metrics must not be reported as the production system's.
"""
from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np
from langchain_core.embeddings import Embeddings

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("the of a an and or to in is are be by for on at as with that this it from any shall which such not".split())


class Embedder(Protocol):
    model_id: str
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_id = f"hashing-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        toks = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        v = np.zeros(self.dim, dtype=np.float32)
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0
        v = np.sqrt(v)  # sublinear tf
        n = np.linalg.norm(v)
        return v / n if n else v

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._vec(t) for t in texts]).astype(np.float32) if texts else np.zeros((0, self.dim), np.float32)


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str):
        if not model_name:
            raise RuntimeError("EMBEDDING_MODEL is not set (see .env.example)")
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy

        self._m = SentenceTransformer(model_name)
        self.model_id = f"st:{model_name}"
        self.dim = int(self._m.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self._m.encode(texts, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False),
            dtype=np.float32,
        )


def get_embedder(backend: str, model_name: str = "") -> Embedder:
    if backend == "hashing":
        return HashingEmbedder()
    if backend == "sentence_transformers":
        return SentenceTransformerEmbedder(model_name)
    raise ValueError(f"Unknown EMBEDDING_BACKEND: {backend}")


class LangChainEmbeddings(Embeddings):
    """Adapter so LangChain's FAISS vector store can use any of our embedders."""

    def __init__(self, embedder: Embedder):
        self.e = embedder

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.e.encode(texts).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.e.encode([text])[0].tolist()
