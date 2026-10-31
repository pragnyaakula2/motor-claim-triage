#!/usr/bin/env bash
# scripts/loadrun.sh <coverage_workers> <users(=rps)> <seconds> <label> [worker_concurrency]
set -u; cd "$(dirname "$0")/.."
N=$1; U=$2; T=$3; L=$4; WC=${5:-4}
export PYTHONPATH=$PWD STUB_LATENCY_MS=800 STUB_JITTER_MS=200 RATE_LIMIT_PER_MIN=100000 RATE_LIMIT_BURST=100000 WORKER_CONCURRENCY=$WC CACHE_ENABLED=false RUN_CACHE_ENABLED=false QUALITY_SAMPLE_RATE=0
scripts/devstack.sh stop >/dev/null 2>&1; sleep 1; scripts/devstack.sh start $N >/dev/null; sleep 3
PY=${PY:-/home/claude/work/venv/bin}
( sleep $((T/2)); top -b -n1 -w 200 | head -16 > loadtest/results/$L.top.txt ) &
$PY/locust -f loadtest/locustfile.py --headless -u $U -r $U -t ${T}s --host http://localhost:8000 --csv loadtest/results/$L --only-summary > loadtest/results/$L.locust.log 2>&1
$PY/python loadtest/summarize.py loadtest/results/$L | tee loadtest/results/$L.summary.json
curl -s localhost:9101/metrics | grep -E '^queue_depth' | tr '\n' ' '; echo
scripts/devstack.sh stop >/dev/null 2>&1
