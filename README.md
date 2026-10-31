# Motor Insurance Claim Intake and Triage (advisory only)

UE24AM342AA5 Mini Project 01. Validates a first notice of loss, triages it, retrieves the relevant policy clauses with citations, verifies the answer, and routes it
(`fast_track` / `surveyor_assignment` / `investigation`) with a reason. **It never approves, rejects or prices a claim.**

## Run it
```
cp .env.example .env              # defaults need no credentials: scripted LLM stub + hashing embedder
docker compose up --build -d --scale agent-worker=3
curl localhost:8000/ready
python scripts/submit.py food     # section 4.1 case; needs `pip install httpx pydantic` locally
```
Open http://localhost:8000 for the web client (a claim form; a JSON tab is available for advanced edits). Without Docker: `pip install -r requirements-dev.txt && scripts/devstack.sh start 3` (needs a local `redis-server`; set `PY` to your venv bin).
Tests: `make test` (80 tests, no network). Stop: `docker compose down`.

## Using a real provider / embeddings
Edit `.env` only: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (any OpenAI-compatible API), `LLM_PRICE_*`; for embeddings set `EMBEDDING_BACKEND=sentence_transformers` and `EMBEDDING_MODEL=<a sentence-transformers model>` and rebuild (`docker compose build retrieval`). No credentials, endpoints or model identifiers are committed. Set a spending limit at your provider first.

## Layout
`claimtriage/` code (api, agents, retrieval, llm, worker) - `prompts/` versioned prompts - `contracts/` JSON schemas - `corpus/` policy PDFs - `evaluation/` scenarios + harness - `loadtest/` Locust - `deploy/` Prometheus/Grafana - `stub/` LLM stub - `legacy/` the earlier prototype - `docs/` report and runbooks.

## Documents
[Report](docs/REPORT.md) - [Architecture](docs/ARCHITECTURE.md) - [Data contract](docs/DATA_CONTRACT.md) - [Design decisions](docs/DESIGN_DECISIONS.md) - [Evaluation](docs/EVALUATION.md) - [SLO](docs/SLO.md) / [Load results](docs/LOAD_RESULTS.md) - [Runbook](docs/RUNBOOK.md) - [Incident log](docs/INCIDENT_LOG.md) - [Demo](docs/DEMO.md)

## Status - read this
Verified here: unit tests, native stack, API end to end, evaluation, load tests (all with the stub). **Not verified here:** Docker Compose run, Grafana/Prometheus/Jaeger, a real LLM, real embeddings, prompt A/B. See `docs/REPORT.md`.
