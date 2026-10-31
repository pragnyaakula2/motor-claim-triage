# Load test results (D6)

Method, target and the **prediction made before measuring** are in `docs/SLO.md` (declared 2026-10-30T01:38:15Z, hash in `docs/slo.declaration.txt`; `SLO.md` is unchanged since).
Conditions: native processes (`scripts/devstack.sh`) on one **2-core** machine shared with Locust; LLM replaced by the stub (800 ms +/- 200 ms/call); hashing embedder;
caches bypassed; one Locust user = 1 run/s open loop; 60-180 s runs. Raw output: `loadtest/results/`. **Docker was not used for these numbers.**

## Scaling effect (3 runs/s offered, worker concurrency 4)
| coverage workers | p50 | p95 | completed runs/s | target met |
|---|---|---|---|---|
| 1 | 29 s | 52 s | 1.17 | no |
| 2 | 12 s | 20 s | 2.31 | no |
| 3 | 4.2 s | 5.1 s | 2.86 | p95 yes (offered load below target) |

Capacity grows roughly linearly with workers until it exceeds the offered load; below it the queue grows and latency explodes.

## Declared target: 5 runs/s, p95 <= 8 s, infra failures <= 1 %
| config | p50 | p95 | runs/s | infra failures | SLO |
|---|---|---|---|---|---|
| 3 workers x concurrency 4 | 16 s | 26 s | 3.47 | 0 | **missed** (as predicted) |
| 3 workers x concurrency 16 | 4.2 s | 5.2 s | 4.77 | 0 | **met** |

My prediction said three workers would miss 5 runs/s unless concurrency was raised and that the bottleneck would be worker concurrency, then CPU. Both held.

## Where it breaks (3 workers x concurrency 16)
| offered runs/s | p95 | completed runs/s | |
|---|---|---|---|
| 8 | 5.2 s | 7.6 | ok |
| 12 | 5.3 s | 11.4 | ok |
| 18 | 24 s | 13.1 | **fails** |
| 25 | 43 s | 12.5 | fails |

The knee is between 12 and 18 runs/s, i.e. a ceiling of about **13 runs/s** on this machine. `top` snapshots at the failing rates show all CPU in use across uvicorn, the three workers and Locust itself, so the limiting component is **CPU** (agent processes doing JSON/validation/retrieval work plus the load generator on the same two cores), not Redis, and not model wait.
This is a property of the test machine; it is not a claim about production capacity. Zero infrastructure failures at every rate: overload shows up as latency, not errors.

## Cost and caching (L5, D7) - notional prices
Prices were set to 0.00015 / 0.0006 USD per 1k input / output tokens **as an illustration, not a quote from any provider**. Source: `loadtest/results/cost_cache_report.json` (`scripts/cost_cache_report.py`, 60 runs per phase, 12 distinct answerable scenarios).
| | value |
|---|---|
| Cost per 100 runs, run cache bypassed | $0.224 |
| Largest cost driver | `coverage:decide` = $0.215 (96 %), about 525 calls and 1.17 M prompt tokens per 100 runs; triage $0.0096 |
| Cost per 100 runs with run cache replay | $0.053 |
| p50 latency, bypassed -> cached | 4.5 s -> 0.02 s |
| p95 latency, bypassed -> cached | 12.5 s -> 13.0 s |
| Retrieval cache | 170 hits / 0 misses in the sample; hit 1.6 ms |

Caching cuts median latency and cost sharply, but **p95 did not improve**: 5 of 60 replayed runs were not served from cache and ran in full, and they set the tail. I did not investigate why those five missed (candidates: results that are escalated are not cached by design, or identical payloads submitted concurrently before the first result was stored); treat that as open. The bypassed-phase p95 (12.5 s) is also higher than the SLO run's 5.2 s; this test used 6 client threads on the same shared 2-core machine and I did not diagnose the difference, so it should not be compared with the SLO table.
The largest cost driver is the coverage agent's repeated decide step re-sending retrieved passages each step; the lever is fewer steps or trimming passages in the prompt, not a cheaper triage.
