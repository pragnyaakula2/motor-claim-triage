# Runbook (L3) - the two most likely failures

All commands assume the compose stack. Without Docker use `scripts/devstack.sh` and read logs in `.devstack/*.log`.

## 1. LLM provider outage / slowness (breaker open, runs degrade)
**Why this is likely:** a hosted provider is the only dependency outside our control; timeouts, 429s and 5xx are routine.

**Symptoms**
* Grafana: *LLM circuit breaker* panel at 2 (open); *Escalations by reason* shows `PROVIDER_UNAVAILABLE`; *Degraded runs* rising; p95 latency may first spike (retries) then fall (breaker short-circuits).
* Callers get results whose `status` is `escalated`, `escalation.code = PROVIDER_UNAVAILABLE`, with a retrieval-only evidence pack and no coverage position.

**Confirm**
```
curl -s localhost:8000/metrics | grep -E "llm_circuit_breaker_state|llm_calls_total|degraded_runs_total"
docker compose logs --since 5m agent-worker triage-agent | grep -Ei "breaker|timeout|429|provider"
curl -s localhost:8200/admin/fault        # only when using the stub: shows an injected fault
```
Then check the provider's status page and the account's spending limit - a hit limit looks exactly like an outage.

**Recover**
1. Nothing is lost: affected runs were answered in degraded form. Re-submit the claims you care about after recovery with `X-Bypass-Cache: 1` (degraded results are not cached).
2. Fix the cause (credentials/limit/base URL in `.env`), then `docker compose up -d --force-recreate agent-worker triage-agent`.
3. The breaker half-opens after `BREAKER_RECOVERY_S` (20 s) and closes after a successful probe; confirm state returns to 0.
4. If the provider is down for long, raise nothing: handlers keep receiving evidence packs. To switch provider, change `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL` only.
Demo: `scripts/fault_demo.sh`.

## 2. Queue backlog / stalled worker
**Why this is likely:** work is queued and workers are the throughput limit (see `docs/LOAD_RESULTS.md`: three workers at concurrency 4 miss the SLO).

**Symptoms**
* *Queue depth* grows and does not drain; *Queue wait p95* climbs; end-to-end p95 rises while model latency is flat; `GET /v1/runs/{id}` stays `queued`/`running`.
* A crashed worker leaves messages **pending** (delivered, never acknowledged).

**Confirm**
```
curl -s localhost:8000/ready                        # includes queue_depth
docker compose ps agent-worker triage-agent         # are replicas up/healthy?
docker compose exec redis redis-cli XINFO GROUPS coverage      # lag = not yet delivered, pending = delivered, unacked
docker compose exec redis redis-cli XPENDING coverage workers - + 5
docker compose logs --since 5m agent-worker | grep -Ei "error|traceback|claimed"
```
`lag` high, `pending` low -> not enough capacity. `pending` high with idle consumers -> stalled/crashed consumers.

**Recover**
1. Capacity: `docker compose up -d --scale agent-worker=6` (and/or raise `WORKER_CONCURRENCY`; both are safe, consumers share one group).
2. Stalled consumers: restart them (`docker compose restart agent-worker`). Pending messages idle longer than `STREAM_CLAIM_IDLE_MS` (60 s) are re-claimed automatically; a message delivered 3 times is escalated `AGENT_FAILURE` instead of looping forever.
3. Do not flush Redis: it holds queued runs (AOF on) and the run store.
4. After recovery confirm queue depth reaches 0 and the SLO panels recover.
