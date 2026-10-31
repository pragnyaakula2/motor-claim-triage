"""Run trace: every LLM call, tool call, query, passage, prompt version, latency, cost and verification outcome (D5)."""
from __future__ import annotations

import threading
import time
from typing import Callable


class TraceRecorder:
    def __init__(self, run_id: str, sink: Callable[[dict], None] | None = None):
        self.run_id = run_id
        self.events: list[dict] = []
        self._sink = sink
        self._lock = threading.Lock()

    def emit(self, agent: str, type_: str, step: str, **fields) -> dict:
        with self._lock:
            ev = {"seq": len(self.events) + 1, "ts": round(time.time(), 3), "run_id": self.run_id,
                  "agent": agent, "type": type_, "step": step, **fields}
            self.events.append(ev)
        if self._sink:
            try:
                self._sink(ev)
            except Exception:  # tracing must never break a run
                pass
        return ev

    def totals(self) -> dict:
        pt = sum(e.get("prompt_tokens", 0) for e in self.events)
        ct = sum(e.get("completion_tokens", 0) for e in self.events)
        cost = sum(e.get("cost_usd", 0.0) for e in self.events)
        by_step: dict[str, dict] = {}
        for e in self.events:
            if e["type"] == "llm_call":
                k = f'{e["agent"]}:{e["step"]}'
                d = by_step.setdefault(k, {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "calls": 0})
                d["prompt_tokens"] += e.get("prompt_tokens", 0)
                d["completion_tokens"] += e.get("completion_tokens", 0)
                d["cost_usd"] += e.get("cost_usd", 0.0)
                d["calls"] += 1
        return {"prompt_tokens": pt, "completion_tokens": ct, "cost_usd": round(cost, 6),
                "llm_calls": sum(1 for e in self.events if e["type"] == "llm_call"),
                "tool_calls": sum(1 for e in self.events if e["type"] == "tool_call"),
                "by_step": by_step}
