#!/usr/bin/env bash
# Demo of graceful degradation (L3/A5): break the LLM provider, show the system still answers, then recover.
# Needs a running stack on localhost:8000 and the stub on :8200 (docker compose up, or scripts/devstack.sh start 2).
set -u; cd "$(dirname "$0")/.."
PY=${PY:-python}
fault() { curl -s -XPOST localhost:8200/admin/fault -H 'content-type: application/json' -d "$1" >/dev/null; }
echo "== 1. provider DOWN: run still returns a retrieval-only evidence pack, escalated PROVIDER_UNAVAILABLE"
fault '{"mode":"down"}'; $PY scripts/submit.py food | sed -n 1,12p
echo "== 2. breaker state after repeated failures"; for i in 1 2 3 4 5 6; do $PY scripts/submit.py normal 2>&1 | grep -E "status=|\"code\"" | tr '\n' ' '; echo; done
echo "(breaker state is per worker process: see the 'LLM circuit breaker' Grafana panel, or worker /metrics on :9100)"
echo "waiting 21 s for the breaker to half-open"; fault '{"mode":"none"}'; sleep 21
echo "== 3. malformed model output (garbage): schema validation + retry, then escalate LLM_OUTPUT_INVALID if it persists"
fault '{"mode":"garbage"}'; $PY scripts/submit.py normal | sed -n 1,6p
echo "== 4. model invents a quote (hallucinate_once): verifier rejects the first draft, second attempt passes"
fault '{"mode":"hallucinate_once"}'; $PY scripts/submit.py food | grep -E "status|attempt|verif" | head
echo "== 5. recover"; fault '{"mode":"none"}'; sleep 21; $PY scripts/submit.py normal | sed -n 1,3p
