"""Prometheus metrics shared by every service (A6, D5, D2, D7, L5)."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

LAT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

# --- HTTP edge -----------------------------------------------------------
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["service", "route"], buckets=LAT_BUCKETS)

# --- ingestion boundary -----------------------------------------------------
INGEST = Counter("fnol_ingest_total", "FNOL submissions by outcome", ["outcome"])  # accepted|quarantined|rate_limited|guardrail_blocked|cache_hit
QUARANTINED = Counter("fnol_quarantined_total", "Quarantined records by reason", ["reason"])

# --- runs / agents (A6) ---------------------------------------------------------
RUNS = Counter("agent_runs_total", "Agent runs by outcome", ["agent", "outcome"])  # completed|escalated|failed|degraded
RUN_STEPS = Histogram("agent_steps_per_run", "LLM decision steps per run", ["agent"], buckets=(1, 2, 3, 4, 5, 6, 8, 10, 15))
RUN_TOOLCALLS = Histogram("agent_tool_calls_per_run", "Tool calls per run", ["agent"], buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10))
AGENT_RUN_LATENCY = Histogram("agent_run_duration_seconds", "Per-agent run latency", ["agent"], buckets=LAT_BUCKETS)
STEP_LATENCY = Histogram("agent_step_duration_seconds", "Per-step latency", ["agent", "step"], buckets=LAT_BUCKETS)
E2E_LATENCY = Histogram("run_e2e_duration_seconds", "Submission to final result", ["outcome"], buckets=LAT_BUCKETS)
RETRIES = Counter("agent_retries_total", "Retries (LLM, tool, verification)", ["agent", "kind"])
STEPS_TOTAL = Counter("agent_steps_total", "Steps executed", ["agent", "step"])
VERIFY = Counter("verification_total", "Verification outcomes", ["outcome"])  # passed|failed
ESCALATIONS = Counter("escalations_total", "Escalations to a human", ["reason"])
ROUTES = Counter("routes_total", "Routing decisions", ["route"])

# --- LLM / tools (A5, L5) ------------------------------------------------------------
LLM_CALLS = Counter("llm_calls_total", "LLM calls", ["agent", "step", "outcome"])
LLM_LATENCY = Histogram("llm_call_duration_seconds", "LLM call latency", ["agent"], buckets=LAT_BUCKETS)
LLM_TOKENS = Counter("llm_tokens_total", "Tokens", ["agent", "step", "kind"])  # prompt|completion
LLM_COST = Counter("llm_cost_usd_total", "Notional cost (USD)", ["agent", "step"])
BREAKER_STATE = Gauge("llm_circuit_breaker_state", "0=closed 1=half_open 2=open")
BREAKER_TRIPS = Counter("llm_circuit_breaker_trips_total", "Times the breaker opened")
DEGRADED = Counter("degraded_runs_total", "Runs served via fallback path", ["path"])
TOOL_CALLS = Counter("tool_calls_total", "Tool calls", ["tool", "outcome"])
TOOL_LATENCY = Histogram("tool_call_duration_seconds", "Tool latency", ["tool"], buckets=LAT_BUCKETS)

# --- guardrails (L4) ----------------------------------------------------------------------
GUARD = Counter("guardrail_events_total", "Guardrail events", ["kind", "action"])

# --- cache / rate limit (D7) ---------------------------------------------------------------------
CACHE = Counter("cache_requests_total", "Cache lookups", ["cache", "result"])

# --- queue ---------------------------------------------------------------------------------------------
QUEUE_DEPTH = Gauge("queue_depth", "Pending messages", ["stream"])
QUEUE_WAIT = Histogram("queue_wait_seconds", "Time a message waits before pickup", ["stream"], buckets=LAT_BUCKETS)
IN_FLIGHT = Gauge("worker_in_flight", "Runs in flight in this worker", ["role"])

# --- pipeline freshness (D2) -----------------------------------------------------------------------------
INDEX_BUILT_TS = Gauge("index_built_timestamp_seconds", "Unix time the index was built")
INDEX_CHUNKS = Gauge("index_chunk_count", "Chunks in the live index")
INDEX_AGE = Gauge("index_age_seconds", "Seconds since the index was built")
PIPE_THROUGHPUT = Gauge("pipeline_chunks_per_second", "Last pipeline throughput (chunks embedded per second)")
PIPE_DURATION = Gauge("pipeline_duration_seconds", "Last pipeline duration")
PIPE_REEMBEDDED = Gauge("pipeline_reembedded_chunks", "Chunks re-embedded in last run")

# --- quality / feedback (L6) ---------------------------------------------------------------------------------
FEEDBACK = Counter("feedback_total", "Caller feedback", ["verdict"])
QUALITY = Gauge("quality_score", "Rolling auto-scored quality", ["metric"])

# --- liveness ------------------------------------------------------------------------------------------------------
UP = Gauge("service_up", "1 while running", ["service"])


def start_metrics_server(port: int, service: str) -> None:
    """Workers have no HTTP app, so expose /metrics from a side thread."""
    start_http_server(port)
    UP.labels(service=service).set(1)
