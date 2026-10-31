# Demo checklist

Start: `cp .env.example .env && docker compose up --build -d --scale agent-worker=3` (fallback: `scripts/devstack.sh start 3`). Web client: http://localhost:8000. Grafana :3000, Prometheus :9090, Jaeger :16686.

Rows are capabilities; map them to the numbering in your brief's demo section.
| Show | How |
|---|---|
| Valid request served end to end (4.1 case) | `python scripts/submit.py food` or the web client's sample button |
| Ordinary claim, fast track | `python scripts/submit.py normal` |
| Bad record quarantined with reasons | POST an invalid payload (e.g. future loss date); then `curl localhost:8000/v1/quarantine` |
| Run trace: steps, tools, retrieved clauses, verifier, router rule | `curl localhost:8000/v1/runs/<id>/trace` |
| Prompt injection flagged, forced to investigation | submit a claim whose description says "ignore all instructions" |
| Scaling | `docker compose up -d --scale agent-worker=3`; Grafana per-replica panels; `docker compose ps` |
| Provider outage, degradation, breaker, recovery | `scripts/fault_demo.sh` |
| Rate limit | burst > 10 requests quickly from one `X-Client-Id`; HTTP 429 |
| Caching | POST the same payload twice with curl (`scripts/submit.py` always bypasses the cache): second returns `cached: true` |
| Evaluation and before/after | `python -m evaluation.compare cov_v0 cov_v1` |
| Load test | `docker compose --profile load up locust` (UI :8089) or `scripts/loadrun.sh 3 5 120 demo 16` |
