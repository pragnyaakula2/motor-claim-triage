"""Environment-driven configuration (D4).

Nothing environment-specific (credentials, endpoints, model identifiers) is
hard-coded anywhere in the repository. Every value is read from the process
environment at runtime; ``.env.example`` documents each variable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if val is None or (required and val == ""):
        raise RuntimeError(f"Missing required environment variable: {name} (see .env.example)")
    return val


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- infrastructure -------------------------------------------------
    service_name: str = field(default_factory=lambda: _env("SERVICE_NAME", "claimtriage"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    retrieval_url: str = field(default_factory=lambda: _env("RETRIEVAL_URL", "http://localhost:8100"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    corpus_dir: Path = field(default_factory=lambda: Path(_env("CORPUS_DIR", "./corpus")))
    prompts_dir: Path = field(default_factory=lambda: Path(_env("PROMPTS_DIR", "./prompts")))
    metrics_port: int = field(default_factory=lambda: _int("METRICS_PORT", 9100))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    otlp_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT", ""))

    # --- embeddings / index (L1, D2) ------------------------------------
    embedding_backend: str = field(default_factory=lambda: _env("EMBEDDING_BACKEND", "sentence_transformers"))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", ""))
    chunk_target_chars: int = field(default_factory=lambda: _int("CHUNK_TARGET_CHARS", 900))
    retrieval_min_score: float = field(default_factory=lambda: _float("RETRIEVAL_MIN_SCORE", 0.0))

    # --- LLM provider (placed behind our own interface) ------------------
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai_compat"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", ""))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 20.0))
    llm_max_retries: int = field(default_factory=lambda: _int("LLM_MAX_RETRIES", 2))
    llm_backoff_base_s: float = field(default_factory=lambda: _float("LLM_BACKOFF_BASE_S", 0.5))
    breaker_failures: int = field(default_factory=lambda: _int("BREAKER_FAILURE_THRESHOLD", 5))
    breaker_recovery_s: float = field(default_factory=lambda: _float("BREAKER_RECOVERY_S", 20.0))
    price_in_per_1k: float = field(default_factory=lambda: _float("LLM_PRICE_IN_PER_1K_USD", 0.0))
    price_out_per_1k: float = field(default_factory=lambda: _float("LLM_PRICE_OUT_PER_1K_USD", 0.0))

    # --- tools -----------------------------------------------------------
    tool_timeout_s: float = field(default_factory=lambda: _float("TOOL_TIMEOUT_S", 8.0))
    tool_max_retries: int = field(default_factory=lambda: _int("TOOL_MAX_RETRIES", 2))

    # --- agent loop bounds (A1) -------------------------------------------
    agent_max_steps: int = field(default_factory=lambda: _int("AGENT_MAX_STEPS", 8))
    agent_max_tool_calls: int = field(default_factory=lambda: _int("AGENT_MAX_TOOL_CALLS", 12))
    agent_deadline_s: float = field(default_factory=lambda: _float("AGENT_DEADLINE_S", 45.0))
    agent_max_attempts: int = field(default_factory=lambda: _int("AGENT_MAX_VERIFY_ATTEMPTS", 3))
    run_deadline_s: float = field(default_factory=lambda: _float("RUN_DEADLINE_S", 90.0))
    verifier_llm_check: bool = field(default_factory=lambda: _bool("VERIFIER_LLM_CHECK", False))

    # --- prompt versions (L2) ----------------------------------------------
    prompt_triage: str = field(default_factory=lambda: _env("PROMPT_VERSION_TRIAGE", "v1"))
    prompt_coverage: str = field(default_factory=lambda: _env("PROMPT_VERSION_COVERAGE", "v1"))

    # --- routing thresholds (F6) ----------------------------------------------
    fast_track_max_inr: float = field(default_factory=lambda: _float("FAST_TRACK_MAX_INR", 20000))
    late_report_days: int = field(default_factory=lambda: _int("LATE_REPORT_DAYS", 7))
    weak_retrieval_threshold: float = field(default_factory=lambda: _float("WEAK_RETRIEVAL_THRESHOLD", 0.2))

    # --- queue / worker -----------------------------------------------------
    worker_role: str = field(default_factory=lambda: _env("WORKER_ROLE", "coverage"))
    worker_concurrency: int = field(default_factory=lambda: _int("WORKER_CONCURRENCY", 4))
    stream_claim_idle_ms: int = field(default_factory=lambda: _int("STREAM_CLAIM_IDLE_MS", 60000))
    run_ttl_s: int = field(default_factory=lambda: _int("RUN_TTL_S", 86400))

    # --- caching / rate limiting (D7) ---------------------------------------------
    cache_enabled: bool = field(default_factory=lambda: _bool("CACHE_ENABLED", True))
    run_cache_enabled: bool = field(default_factory=lambda: _bool("RUN_CACHE_ENABLED", True))
    cache_ttl_s: int = field(default_factory=lambda: _int("CACHE_TTL_S", 3600))
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 30))
    rate_limit_burst: int = field(default_factory=lambda: _int("RATE_LIMIT_BURST", 10))

    # --- guardrails (L4) -------------------------------------------------------------
    injection_block_score: float = field(default_factory=lambda: _float("INJECTION_BLOCK_SCORE", 0.8))
    injection_flag_score: float = field(default_factory=lambda: _float("INJECTION_FLAG_SCORE", 0.4))

    # --- quality sampling (L6) ------------------------------------------------------------
    quality_sample_rate: float = field(default_factory=lambda: _float("QUALITY_SAMPLE_RATE", 0.25))

    # --- stub (load test) -------------------------------------------------------------------
    stub_latency_ms: int = field(default_factory=lambda: _int("STUB_LATENCY_MS", 800))
    stub_jitter_ms: int = field(default_factory=lambda: _int("STUB_JITTER_MS", 200))
    stub_mode: str = field(default_factory=lambda: _env("STUB_MODE", "scripted"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
