# Report - Motor Insurance Claim Intake and Triage

## What was built
Advisory-only multi-agent service: validated FNOL in; triage agent; coverage agent with bounded tool loop over a clause-aware hybrid index; deterministic verifier and router; every output is a cited recommendation for a human, never a decision.
Architecture: `docs/ARCHITECTURE.md`. Contract: `docs/DATA_CONTRACT.md`. Decisions: `docs/DESIGN_DECISIONS.md`. Operations: `docs/RUNBOOK.md`, `docs/INCIDENT_LOG.md`. Measurements: `docs/EVALUATION.md`, `docs/LOAD_RESULTS.md`, `docs/SLO.md`.

## Headline findings
1. A single retrieval pass misses the structural clauses for the section 4.1 case (context recall 0.29); derived queries plus cross-reference chasing raise it to 0.94 with the stub. The permitted-use restriction text lives in a Schedule that is not in the corpus, so the system correctly reports "permitted use unverifiable" and routes to investigation.
2. The declared SLO (5 runs/s, p95 <= 8 s) was missed at first (3 workers x concurrency 4) and met after raising worker concurrency to 16. Ceiling on the 2-core test box is about 13 runs/s, CPU-bound.
3. Run caching cuts cost per 100 runs from $0.22 to $0.05 (notional prices) and median latency to near zero, but did not improve p95.

## What did not work / is not verified
* **E08 (total loss after a fire) fails**: three verification attempts end in `VERIFICATION_FAILED`, with zero gold-anchor recall. Escalating is the safe behaviour, but it is a miss on an answerable claim. Not diagnosed beyond that.
* **The verifier does not catch a missed applicable condition.** It checks that what is cited is real and that restriction positions cite a restriction; it cannot know a relevant condition was never retrieved. Recall (0.94) is the only guard.
* **Metrics are lexical proxies** and the hallucination rate swung 0.19 -> 0.06 between runs of one configuration.
* **No scenario covers geographic restrictions** of cover.
* **Corpus gap:** the Schedule / "Limitations as to Use" text is absent. Add it to `corpus/` and re-run before relying on 4.1 results; confirm the 4.1 reference answer with your supervisor.
* **The claims-procedure source is a web-page print**, noisy and needing cleaning rules (incidents 1-2); a real procedure document would be better.
* **Everything measured used the scripted stub and hashing embedder.** Never run here: any real LLM provider, real sentence-transformers embeddings, the L2 prompt A/B, real-model hallucination behaviour.
* **Docker was not run** (no daemon here). `docker compose config` validates the file; images, health checks, `--scale agent-worker=3`, Prometheus DNS discovery, the Grafana dashboard and Jaeger export are **untested**. Load numbers are from native processes.
* Defaults worth stating: `WEAK_RETRIEVAL_THRESHOLD=0.2`, fast-track limit Rs.20,000.

## Before the demo (the system must start and serve, or it scores zero)
Run `docker compose up --build -d` on your own machine at least once, then `python scripts/submit.py food`. If anything fails, fall back to `scripts/devstack.sh start 3`, which was tested.
