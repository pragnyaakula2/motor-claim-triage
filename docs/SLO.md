# Service level objective - declared BEFORE any load measurement (D6)

Declared: see `docs/slo.declaration.txt` (UTC timestamp + SHA-256 of this file, written before the first Locust run).

## What is measured
One **run** = submission (`POST /v1/claims` accepted) -> final result readable on `GET /v1/runs/{id}` with status
`completed` or `escalated`. The clock starts when the client sends the POST and stops when the poll sees a terminal status
(Locust records it as the custom request `RUN e2e`). The submission call alone is reported separately and is **not** the SLO.

## Test conditions (fixed in advance)
- LLM replaced by `llm-stub` (fixed scripted responses after a delay): **800 ms +/- 200 ms per call**, i.e. a small hosted model.
  A run makes about 5 calls (1 triage + ~4 coverage steps), so about **4.0 s of the budget is unavoidable model time**.
- Embeddings and FAISS local (no network). Cache disabled for the SLO run (`X-Bypass-Cache`) so the target is not flattered.
- Workload: the section 4.1 claim and ordinary claims mixed 1:4. Arrival model: open loop, each Locust user submits 1 run/s.
- Rate limit raised for the test client only (rate limiting is demonstrated separately).

## Declared target
| | |
|---|---|
| **Target RPS (sustained 3 min)** | **5 runs/s** |
| **p95 end-to-end latency** | **<= 8 s** (about 4 s model time + <= 4 s queueing and system overhead) |
| p50 end-to-end latency | <= 6 s (informative, not the SLO) |
| Infrastructure failure rate | <= 1 % of runs (HTTP 5xx, runs that never reach a terminal state, `AGENT_FAILURE`, `RUN_TIMEOUT`) |

## Why these numbers (section 1 of the brief)
Intake is high-volume and latency-sensitive: a handler or call-centre agent wants a routed recommendation while the customer is
still on the line, and an attention window of roughly ten seconds is the practical ceiling. 8 s keeps a complete multi-step run inside it with
margin. 5 runs/s is about 18,000 claims/hour: well above the FNOL peak of a mid-sized motor insurer, so passing it means headroom.
The target is expressed *over* the stubbed model time on purpose: it bounds the overhead this system adds. With a real provider,
the same system overhead budget (<= 4 s) applies on top of the provider's own latency, which this test cannot and does not claim to measure.

## Prediction recorded before measuring
With one `agent-worker` (4 threads) throughput is limited by thread count: 4 concurrent runs x (1 / ~4.5 s) ~ 0.9 runs/s, so a single worker is expected to
**miss** 5 runs/s. Three workers (12 threads) give ~2.7 runs/s of pure model-wait capacity, which is still below 5 runs/s, so the target is expected to fail
at three workers unless `WORKER_CONCURRENCY` is raised; the bottleneck is expected to be worker concurrency (I/O wait), then CPU on the two-core test machine.
