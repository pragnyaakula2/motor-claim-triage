"""Retrieval service: the agent's search/lookup tools over HTTP (resource-heavy, scaled independently)."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .. import metrics as M
from ..config import get_settings
from ..contracts import CrossRefResponse, Passage, SearchRequest, SearchResponse
from ..logging_setup import CORRELATION_HEADER, correlation_id, new_id, setup_logging
from ..telemetry import setup_tracing
from .embedder import get_embedder
from .index import PolicyIndex

log = logging.getLogger("retrieval")
state: dict = {}


def load_index() -> PolicyIndex:
    st = get_settings()
    ix = PolicyIndex(st.data_dir / "index", get_embedder(st.embedding_backend, st.embedding_model))
    M.INDEX_BUILT_TS.set(ix.manifest["built_at"])
    M.INDEX_CHUNKS.set(ix.manifest["chunk_count"])
    M.PIPE_THROUGHPUT.set(ix.manifest.get("throughput_chunks_per_s", 0))
    M.PIPE_DURATION.set(ix.manifest.get("pipeline_seconds", 0))
    M.PIPE_REEMBEDDED.set(ix.manifest.get("reembedded_chunks", 0))
    return ix


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = get_settings()
    setup_logging("retrieval", st.log_level)
    setup_tracing("retrieval", st.otlp_endpoint)
    state["index"] = load_index()
    M.UP.labels(service="retrieval").set(1)
    log.info("index %s loaded (%d chunks)", state["index"].version, len(state["index"].chunks))
    yield


app = FastAPI(title="Policy retrieval service", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def mw(request: Request, call_next):
    cid = request.headers.get(CORRELATION_HEADER) or new_id("c-")
    tok = correlation_id.set(cid)
    t0 = time.time()
    try:
        resp = await call_next(request)
    finally:
        correlation_id.reset(tok)
    route = request.scope.get("route").path if request.scope.get("route") else request.url.path
    M.HTTP_REQUESTS.labels("retrieval", route, request.method, resp.status_code).inc()
    M.HTTP_LATENCY.labels("retrieval", route).observe(time.time() - t0)
    resp.headers[CORRELATION_HEADER] = cid
    return resp


def _ix() -> PolicyIndex:
    ix = state.get("index")
    if ix is None:
        raise HTTPException(503, "index not loaded")
    return ix


@app.get("/health")
def health():
    ix = _ix()
    return {"status": "ok", "index_version": ix.version, "chunks": len(ix.chunks)}


@app.get("/index/info")
def info():
    ix = _ix()
    m = dict(ix.manifest)
    m["age_seconds"] = round(time.time() - m["built_at"], 1)
    M.INDEX_AGE.set(m["age_seconds"])
    return m


@app.post("/admin/reload")
def reload():
    state["index"] = load_index()
    return {"reloaded": state["index"].version}


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest):
    ix = _ix()
    res = ix.search(req.query, req.top_k, req.section_type, req.mode)
    return SearchResponse(passages=[Passage(**r) for r in res], index_version=ix.version, mode=req.mode,
                          top_score=max((r["score"] for r in res), default=0.0))


@app.get("/clause/{clause_id:path}", response_model=Passage)
def clause(clause_id: str):
    c = _ix().get_clause(clause_id)
    if not c:
        raise HTTPException(404, f"unknown clause_id {clause_id}")
    return Passage(**c)


@app.get("/crossrefs/{clause_id:path}", response_model=CrossRefResponse)
def crossrefs(clause_id: str):
    r = _ix().cross_refs(clause_id)
    if r is None:
        raise HTTPException(404, f"unknown clause_id {clause_id}")
    return CrossRefResponse(**r)


@app.get("/metrics")
def metrics():
    ix = state.get("index")
    if ix:
        M.INDEX_AGE.set(time.time() - ix.manifest["built_at"])
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
