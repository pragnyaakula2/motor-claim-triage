#!/usr/bin/env bash
# Start the whole system natively (no Docker) for development/testing: redis, stub, retrieval, api, triage + coverage workers.
# Usage: scripts/devstack.sh start|stop|status [N_COVERAGE_WORKERS]
set -u
cd "$(dirname "$0")/.."
PY=${PY:-/home/claude/work/venv/bin}
RUN=.devstack; mkdir -p $RUN
export EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-hashing} DATA_DIR=./data CORPUS_DIR=./corpus PROMPTS_DIR=./prompts
export REDIS_URL=redis://localhost:6379/0 RETRIEVAL_URL=http://localhost:8100 LLM_PROVIDER=openai_compat
export LLM_BASE_URL=${LLM_BASE_URL:-http://localhost:8200/v1} LLM_API_KEY=${LLM_API_KEY:-dev} LLM_MODEL=${LLM_MODEL:-stub-scripted}
start() { name=$1; shift; setsid nohup "$@" > $RUN/$name.log 2>&1 < /dev/null & echo $! > $RUN/$name.pid; }
case "${1:-status}" in
  start)
    N=${2:-1}
    redis-cli ping >/dev/null 2>&1 || start redis redis-server --port 6379 --appendonly no --save ""
    sleep 0.5
    start stub $PY/uvicorn stub.llm_stub:app --port 8200 --log-level warning
    start retrieval $PY/uvicorn claimtriage.retrieval.service:app --port 8100 --log-level warning
    sleep 3
    METRICS_PORT=9101 start api $PY/uvicorn claimtriage.api.main:app --port 8000 --log-level warning
    WORKER_ROLE=triage METRICS_PORT=9110 start triage $PY/python -m claimtriage.worker
    for i in $(seq 1 $N); do WORKER_ROLE=coverage METRICS_PORT=$((9120+i)) start coverage$i $PY/python -m claimtriage.worker; done
    sleep 2; echo started;;
  stop)
    for f in $RUN/*.pid; do [ -f "$f" ] && kill $(cat $f) 2>/dev/null; rm -f $f; done
    redis-cli shutdown nosave 2>/dev/null; echo stopped;;
  status) for f in $RUN/*.pid; do [ -f "$f" ] && echo "$(basename $f .pid): $(kill -0 $(cat $f) 2>/dev/null && echo up || echo DOWN)"; done;;
esac
