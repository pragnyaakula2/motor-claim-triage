"""Provider-agnostic LLM client (A5, L5).

* one interface (``LLMClient.chat``) over any OpenAI-compatible endpoint, chosen by config
* timeout on every call, bounded retries with exponential backoff + jitter
* circuit breaker (closed -> open -> half-open) shared by all calls in the process
* tokens and notional cost recorded at call time and returned to the caller for per-step attribution
* never returns a partial/failed call as a result: it raises, and the agent's fallback path takes over
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

import httpx

from .. import metrics as M
from ..config import Settings, get_settings
from ..logging_setup import correlation_id, log

logger = logging.getLogger("llm")


class ProviderUnavailable(Exception):
    """Breaker open, provider disabled, or retries exhausted."""


class LLMBadResponse(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float
    model: str
    attempts: int = 1


class CircuitBreaker:
    CLOSED, HALF_OPEN, OPEN = 0, 1, 2

    def __init__(self, failure_threshold: int, recovery_s: float):
        self.threshold, self.recovery_s = failure_threshold, recovery_s
        self.state, self.failures, self.opened_at = self.CLOSED, 0, 0.0
        self._lock = threading.Lock()
        self._probing = False
        M.BREAKER_STATE.set(0)

    def allow(self) -> bool:
        with self._lock:
            if self.state == self.OPEN:
                if time.time() - self.opened_at >= self.recovery_s:
                    self.state, self._probing = self.HALF_OPEN, False
                    M.BREAKER_STATE.set(1)
                else:
                    return False
            if self.state == self.HALF_OPEN:
                if self._probing:
                    return False  # only one probe at a time
                self._probing = True
            return True

    def success(self) -> None:
        with self._lock:
            if self.state != self.CLOSED:
                log(logger, logging.WARNING, "circuit breaker closed", event="breaker_closed")
            self.state, self.failures, self._probing = self.CLOSED, 0, False
            M.BREAKER_STATE.set(0)

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            self._probing = False
            if self.state == self.HALF_OPEN or self.failures >= self.threshold:
                if self.state != self.OPEN:
                    M.BREAKER_TRIPS.inc()
                    log(logger, logging.ERROR, "circuit breaker OPENED", event="breaker_open", failures=self.failures)
                self.state, self.opened_at = self.OPEN, time.time()
                M.BREAKER_STATE.set(2)

    @property
    def name(self) -> str:
        return {0: "closed", 1: "half_open", 2: "open"}[self.state]


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


class LLMClient:
    def __init__(self, settings: Settings | None = None, breaker: CircuitBreaker | None = None):
        self.s = settings or get_settings()
        self.breaker = breaker or CircuitBreaker(self.s.breaker_failures, self.s.breaker_recovery_s)
        self._http = httpx.Client(timeout=httpx.Timeout(self.s.llm_timeout_s, connect=3.0))

    @property
    def enabled(self) -> bool:
        return self.s.llm_provider != "none" and bool(self.s.llm_base_url)

    def chat(self, messages: list[dict], *, agent: str, step: str, max_tokens: int = 700,
             temperature: float = 0.0, deadline: float | None = None) -> LLMResult:
        if not self.enabled:
            M.LLM_CALLS.labels(agent, step, "disabled").inc()
            raise ProviderUnavailable("LLM provider disabled by configuration")
        if not self.breaker.allow():
            M.LLM_CALLS.labels(agent, step, "breaker_open").inc()
            raise ProviderUnavailable("circuit breaker open")

        url = self.s.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.s.llm_api_key}", "X-Correlation-ID": correlation_id.get()}
        body = {"model": self.s.llm_model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "response_format": {"type": "json_object"}}
        last: Exception | None = None
        for attempt in range(self.s.llm_max_retries + 1):
            if deadline is not None and time.time() >= deadline:
                raise ProviderUnavailable("deadline reached before LLM call")
            t0 = time.time()
            try:
                timeout = self.s.llm_timeout_s if deadline is None else max(0.5, min(self.s.llm_timeout_s, deadline - time.time()))
                r = self._http.post(url, json=body, headers=headers, timeout=timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                if r.status_code >= 400:
                    # client error: not retryable, not a provider outage
                    M.LLM_CALLS.labels(agent, step, "client_error").inc()
                    raise LLMBadResponse(f"provider returned {r.status_code}: {r.text[:200]}")
                data = r.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or sum(_est_tokens(m["content"]) for m in messages))
                ct = int(usage.get("completion_tokens") or _est_tokens(text))
                cost = pt / 1000 * self.s.price_in_per_1k + ct / 1000 * self.s.price_out_per_1k
                dt = time.time() - t0
                self.breaker.success()
                M.LLM_CALLS.labels(agent, step, "ok").inc()
                M.LLM_LATENCY.labels(agent).observe(dt)
                M.LLM_TOKENS.labels(agent, step, "prompt").inc(pt)
                M.LLM_TOKENS.labels(agent, step, "completion").inc(ct)
                M.LLM_COST.labels(agent, step).inc(cost)
                return LLMResult(text, pt, ct, cost, dt, data.get("model", self.s.llm_model), attempt + 1)
            except LLMBadResponse:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, KeyError, ValueError) as e:
                last = e
                self.breaker.failure()
                M.LLM_CALLS.labels(agent, step, "error").inc()
                M.RETRIES.labels(agent, "llm").inc()
                log(logger, logging.WARNING, "llm call failed", event="llm_error", attempt=attempt + 1,
                    error=type(e).__name__, detail=str(e)[:120], breaker=self.breaker.name)
                if self.breaker.state == CircuitBreaker.OPEN:
                    break
                if attempt < self.s.llm_max_retries:
                    time.sleep(self.s.llm_backoff_base_s * (2 ** attempt) * (0.5 + random.random()))
        raise ProviderUnavailable(f"LLM call failed after retries: {type(last).__name__}: {last}")
