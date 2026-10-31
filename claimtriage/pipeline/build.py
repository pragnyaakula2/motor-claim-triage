"""D2: idempotent corpus pipeline - read -> parse -> chunk -> embed -> index.

    python -m claimtriage.pipeline.build                  # incremental, idempotent
    python -m claimtriage.pipeline.build --force-source "Motor Insurance Claims.pdf"
    python -m claimtriage.pipeline.build --full           # re-embed everything

Embeddings are cached on disk under a key = sha256(model_id + embed_text). Re-running
with unchanged inputs embeds nothing; changing one document (or ``--force-source``)
re-embeds only that document's chunks; the (cheap) FAISS index is then re-assembled
from cached vectors. Freshness and throughput are written to ``manifest.json`` and
exposed as Prometheus metrics by the retrieval service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

import faiss
import numpy as np
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from pypdf import PdfReader

from ..config import get_settings
from ..logging_setup import setup_logging
from ..retrieval.chunker import CHUNKER_VERSION, chunk_document
from ..retrieval.embedder import LangChainEmbeddings, get_embedder

log = logging.getLogger("pipeline")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def run(force_sources: list[str] | None = None, full: bool = False) -> dict:
    st = get_settings()
    store = st.data_dir / "index"
    store.mkdir(parents=True, exist_ok=True)
    profile = json.loads((st.corpus_dir / "profile.json").read_text())
    docs = profile["documents"]
    t0 = time.time()

    embedder = get_embedder(st.embedding_backend, st.embedding_model)

    # ---- read + parse + chunk ------------------------------------------------
    chunks, sources = [], []
    for name, prof in docs.items():
        path = st.corpus_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Corpus file listed in corpus/profile.json is missing: {path}")
        reader = PdfReader(str(path))
        pages = [(i, (pg.extract_text() or "")) for i, pg in enumerate(reader.pages, start=1)]
        if not any(t.strip() for _, t in pages):
            raise RuntimeError(f"{name} has no text layer; run OCR first (see README).")
        cks = chunk_document(pages, name, prof, target=st.chunk_target_chars)
        chunks += cks
        sources.append({"source": name, "sha256": sha256_file(path), "pages": len(pages),
                        "chunks": len(cks), "doc_version": prof["version"]})
        log.info("parsed %s: %d pages -> %d chunks", name, len(pages), len(cks))

    # ---- embed only what is not cached -------------------------------------------
    cache_f = store / "embeddings_cache.npz"
    cache: dict[str, np.ndarray] = {}
    if cache_f.exists() and not full:
        z = np.load(cache_f, allow_pickle=False)
        cache = {k: z[k] for k in z.files}
    force = set(force_sources or [])

    def key(c) -> str:
        return hashlib.sha256((embedder.model_id + "\n" + c.embed_text).encode()).hexdigest()

    todo = [c for c in chunks if key(c) not in cache or c.source in force]
    t_embed = time.time()
    if todo:
        vecs = embedder.encode([c.embed_text for c in todo])
        for c, v in zip(todo, vecs):
            cache[key(c)] = v
    embed_s = max(time.time() - t_embed, 1e-9)

    live = {key(c) for c in chunks}
    cache = {k: v for k, v in cache.items() if k in live}  # drop vectors for chunks that no longer exist
    np.savez(cache_f, **cache)

    # ---- assemble FAISS (cheap) -----------------------------------------------------
    mat = np.vstack([cache[key(c)] for c in chunks]).astype("float32")
    index = faiss.IndexFlatIP(mat.shape[1])
    index.add(mat)
    docstore = InMemoryDocstore({str(i): Document(page_content=c.text, metadata={"clause_id": c.clause_id})
                                 for i, c in enumerate(chunks)})
    vs = FAISS(LangChainEmbeddings(embedder), index, docstore, {i: str(i) for i in range(len(chunks))})
    vs.save_local(str(store / "faiss"), "policy")

    (store / "chunks.json").write_text(json.dumps([c.to_dict() for c in chunks], indent=1))
    index_version = hashlib.sha256(
        (embedder.model_id + CHUNKER_VERSION + "".join(sorted(key(c) for c in chunks))).encode()).hexdigest()[:12]
    total_s = time.time() - t0
    manifest = {
        "index_version": index_version,
        "built_at": time.time(),
        "built_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "embedding_backend": st.embedding_backend,
        "embedding_model_id": embedder.model_id,
        "embedding_dim": int(mat.shape[1]),
        "chunker_version": CHUNKER_VERSION,
        "chunk_target_chars": st.chunk_target_chars,
        "chunk_count": len(chunks),
        "sources": sources,
        "reembedded_chunks": len(todo),
        "reused_chunks": len(chunks) - len(todo),
        "embed_seconds": round(embed_s, 3),
        "pipeline_seconds": round(total_s, 3),
        "throughput_chunks_per_s": round(len(todo) / embed_s, 2) if todo else 0.0,
    }
    (store / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("index %s: %d chunks (%d re-embedded, %d reused) in %.2fs", index_version, len(chunks),
             len(todo), len(chunks) - len(todo), total_s)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force-source", action="append", help="re-embed all chunks of this source file only")
    ap.add_argument("--full", action="store_true", help="ignore the embedding cache and re-embed everything")
    a = ap.parse_args()
    setup_logging("pipeline", get_settings().log_level)
    m = run(a.force_source, a.full)
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
