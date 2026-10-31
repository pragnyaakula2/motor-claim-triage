# Architecture and deployment boundaries (D3)

```
 caller --HTTP--> api --XADD--> [redis stream: fnol] --> triage-agent --XADD--> [stream: coverage] --> agent-worker x N
                   |                                                                                       |
                   +--GET status/trace-------- redis (run store, traces, caches, rate limiter) <-----------+
                                                                          agent-worker --HTTP--> retrieval (FAISS + BM25, read-only)
                                                                          agents       --HTTP--> LLM provider (via our client: timeout, retry, breaker)
 prometheus <- scrapes api, triage-agent, every agent-worker replica (DNS service discovery), retrieval, stub, cadvisor
 grafana <- prometheus      jaeger <- OTLP spans from every Python service
```

## Services and why each is its own container
| Service | Boundary reason |
|---|---|
| `api` | Only component facing callers: validation, guardrails, rate limit, idempotency. Stateless, scales independently, never runs an agent. |
| `triage-agent` | Agent 1 as a **separate process** that shares nothing with agent 2 except Redis messages. A crash or slow model call in one cannot block the other. |
| `agent-worker` | Agent 2 (coverage): the expensive, slow, LLM-bound part. Stateless and unnamed so `--scale agent-worker=3` works; consumer groups spread load; a dead worker's messages are re-claimed (`XAUTOCLAIM`), max 3 deliveries then escalated `AGENT_FAILURE`. |
| `retrieval` | Read-only. Index and embedding model are **baked into the image at build** so the container needs no network and every replica serves an identical index. Updating the corpus = rebuild image (or `POST /admin/reload` after rebuilding the volume). |
| `redis` | One piece of shared state: Streams as queue (durable with AOF), run store, caches, token-bucket rate limiter. |
| `llm-stub` | Provider stand-in with fault injection. Replace by changing three env values; no code change. |
| `prometheus`, `grafana`, `jaeger`, `cadvisor` | Observability. Optional at run time; the service works without them. |

## Agents
* **Triage Agent** - extracts entities from the narrative (rules baseline + LLM), classifies claim type, computes structural risk flags, runs a `scope_check`; produces a structured hand-off.
* **Coverage Agent** - LangGraph loop with four read-only tools (`search_policy`, `get_clause`, `follow_cross_refs`, `list_sections`). Bounded by max steps per attempt (8), tool calls (12), wall-clock (45 s) and 3 verification attempts.
  Strategies tried in order: `semantic_chase`, `keyword_sections`, `minimal_evidence`.
* **Verifier V1-V7** (deterministic): citations resolve; quotes are verbatim; figures supported; modifiers examined; cover and restriction both cited; use restriction addressed; retrieval strength. Optional LLM entailment judge (`VERIFIER_LLM_CHECK`).
* **Router R1-R11** (deterministic, ordered): the model informs the *coverage position*, it never picks the queue. Every route carries a rule id and reason.

## Failure containment
Retrieval down -> tool retries, then escalation `INSUFFICIENT_EVIDENCE`. LLM down/timeouts -> retries with jittered backoff -> breaker opens -> retrieval-only evidence pack, `PROVIDER_UNAVAILABLE`.
Malformed output -> schema check + one repair retry -> `LLM_OUTPUT_INVALID`. Budget exhausted -> `BUDGET_EXHAUSTED`. Whole run over `RUN_DEADLINE_S` -> `RUN_TIMEOUT`. Output claiming a decision -> blocked by the output filter.
The system is **advisory only**: no output states that a claim is approved or rejected.
