import json
import time

import httpx
import pytest

from claimtriage.agents.tools import ToolError, validate_args
from claimtriage.config import Settings
from claimtriage.llm.client import LLMClient, ProviderUnavailable

OK = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def client(handler, **env):
    import os
    os.environ.update({"LLM_PROVIDER": "openai_compat", "LLM_BASE_URL": "http://x/v1", "LLM_MODEL": "m", "LLM_MAX_RETRIES": "2",
                       "LLM_BACKOFF_BASE_S": "0.01", "BREAKER_FAILURE_THRESHOLD": "4", "BREAKER_RECOVERY_S": "0.3",
                       "LLM_PRICE_IN_PER_1K_USD": "0.001", "LLM_PRICE_OUT_PER_1K_USD": "0.002", **env})
    c = LLMClient(Settings())
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


MSG = [{"role": "user", "content": "hi"}]


def test_cost_and_tokens_recorded():
    c = client(lambda r: httpx.Response(200, json=OK))
    r = c.chat(MSG, agent="t", step="s")
    assert (r.prompt_tokens, r.completion_tokens) == (10, 2) and r.cost_usd == pytest.approx(0.000014)


def test_retries_then_succeeds():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(503) if n["i"] < 3 else httpx.Response(200, json=OK)
    r = client(h).chat(MSG, agent="t", step="s")
    assert r.attempts == 3


def test_breaker_opens_then_half_opens_then_closes():
    state = {"down": True}
    c = client(lambda req: httpx.Response(503) if state["down"] else httpx.Response(200, json=OK))
    with pytest.raises(ProviderUnavailable):
        c.chat(MSG, agent="t", step="s")
    with pytest.raises(ProviderUnavailable):
        c.chat(MSG, agent="t", step="s")
    assert c.breaker.name == "open"
    calls_before = c._http._transport  # no network when open
    with pytest.raises(ProviderUnavailable, match="breaker"):
        c.chat(MSG, agent="t", step="s")
    state["down"] = False
    time.sleep(0.35)
    assert c.chat(MSG, agent="t", step="s").text == "{}"
    assert c.breaker.name == "closed"


def test_timeout_is_retried_and_bounded():
    def h(req):
        raise httpx.ReadTimeout("slow")
    with pytest.raises(ProviderUnavailable):
        client(h).chat(MSG, agent="t", step="s")


def test_disabled_provider():
    c = client(lambda r: httpx.Response(200, json=OK), LLM_PROVIDER="none")
    with pytest.raises(ProviderUnavailable):
        c.chat(MSG, agent="t", step="s")


@pytest.mark.parametrize("tool,args", [
    ("rm_rf", {}), ("search_policy", {}), ("search_policy", {"query": "x" * 500}), ("search_policy", {"query": "ok query", "section_type": "secret"}),
    ("get_clause", {"clause_id": "../../etc/passwd"}), ("search_policy", {"query": "ok query", "evil": 1}),
    ("get_claim_fields", {"fields": ["driver.password"]}),
])
def test_tool_args_rejected(tool, args):
    with pytest.raises(ToolError):
        validate_args(tool, args)


def test_tool_args_clamped():
    assert validate_args("search_policy", {"query": " ok query ", "top_k": 99})["top_k"] == 8
    assert validate_args("get_clause", {"clause_id": "PCPP:GE:01"})
