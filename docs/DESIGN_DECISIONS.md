# Design decisions and what was rejected (D8)

| Decision | Chosen | Rejected, and why |
|---|---|---|
| Chunking | Clause-aware: section boundaries kept, role assigned, cross references extracted | Fixed-size windows: split an exclusion from its heading and separated "except as stated in..." qualifiers from the clause they modify. |
| Retrieval | Dense FAISS + BM25 fused with RRF | Dense only: missed exact clause identifiers and defined terms (the "section 4.1" structural miss). Measured with the naive single-pass baseline (context recall 0.29 vs 0.94). |
| Cross references | Resolved explicitly; unresolved ones surfaced as a finding | Ignoring them: the permitted-use restriction lives in a Schedule that is not in the corpus. The system reports that gap rather than guessing. |
| Orchestration | Separate processes + Redis Streams | In-process function calls between agents: no isolation, no independent scaling, and `--scale` would not mean anything. A broker such as Kafka/RabbitMQ: more moving parts than a 5 runs/s workload needs; Streams give consumer groups, redelivery and AOF durability in a service already needed. |
| Agent framework | LangGraph for the coverage loop only | A framework for everything: the triage agent and verifier are deterministic enough that a graph adds no value. |
| Routing | Deterministic rules with reasons | Model-picked route: not auditable, not testable, drifts with prompts. |
| Verification | Deterministic V1-V7, optional LLM judge | LLM judge as the only check: grades its own family of errors; kept optional and off by default. |
| Provider integration | One OpenAI-compatible client behind our interface, with timeout, retry, breaker, cost tracking | A vendor SDK in agent code: vendor lock-in and untestable failure modes. |
| Result delivery | Async: 202 + poll | Synchronous request: holds an HTTP connection for 4-60 s and couples client timeouts to model latency. |
| Evaluation metrics | Lexical proxies defined in `evalmetrics.py` | A model-graded metric: would need the real provider and would make the metric move when the provider changes. Limits stated in `docs/EVALUATION.md`. |
| Load-test model | Scripted stub with fixed latency | Real provider: unrepeatable, costs money, rate-limited, and would measure the vendor. The SLO is therefore expressed as *our* overhead on top of model time. |
