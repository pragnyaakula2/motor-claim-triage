"""Redis-backed state: run records, traces, quarantine, feedback, quality, caches, rate limits, queues (A4, D7, L6)."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from typing import Any

import redis

from . import metrics as M
from .config import get_settings

STREAM_INTAKE = "stream:intake"
STREAM_COVERAGE = "stream:coverage"
GROUP_INTAKE = "g:triage"
GROUP_COVERAGE = "g:coverage"


def connect() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=3)


class Store:
    def __init__(self, r: redis.Redis | None = None):
        self.r = r or connect()
        self.s = get_settings()

    # ---- runs --------------------------------------------------------------
    def create_run(self, run_id: str, claim: dict, meta: dict) -> None:
        now = time.time()
        rec = {"run_id": run_id, "status": "queued", "submitted_at": now, "updated_at": now,
               "claim": json.dumps(claim), "meta": json.dumps(meta)}
        self.r.hset(f"run:{run_id}", mapping=rec)
        self.r.expire(f"run:{run_id}", self.s.run_ttl_s)

    def update_run(self, run_id: str, **fields) -> None:
        mapping = {k: (json.dumps(v) if not isinstance(v, (str, int, float)) else v) for k, v in fields.items()}
        mapping["updated_at"] = time.time()
        self.r.hset(f"run:{run_id}", mapping=mapping)

    def get_run(self, run_id: str) -> dict | None:
        d = self.r.hgetall(f"run:{run_id}")
        if not d:
            return None
        for k in ("claim", "meta", "handoff", "result"):
            if k in d:
                d[k] = json.loads(d[k])
        d["submitted_at"] = float(d["submitted_at"])
        d["updated_at"] = float(d["updated_at"])
        return d

    def incr_field(self, run_id: str, field: str) -> int:
        return int(self.r.hincrby(f"run:{run_id}", field, 1))

    # ---- trace ---------------------------------------------------------------
    def append_event(self, run_id: str, ev: dict) -> None:
        k = f"trace:{run_id}"
        self.r.rpush(k, json.dumps(ev, default=str))
        self.r.expire(k, self.s.run_ttl_s)

    def get_events(self, run_id: str) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange(f"trace:{run_id}", 0, -1)]

    # ---- quarantine ------------------------------------------------------------------
    def quarantine(self, rec: dict) -> None:
        self.r.hset("quarantine", rec["quarantine_id"], json.dumps(rec, default=str))
        self.r.lpush("quarantine:index", rec["quarantine_id"])
        self.r.ltrim("quarantine:index", 0, 4999)

    def get_quarantine(self, qid: str) -> dict | None:
        v = self.r.hget("quarantine", qid)
        return json.loads(v) if v else None

    def list_quarantine(self, n: int = 50) -> list[dict]:
        ids = self.r.lrange("quarantine:index", 0, n - 1)
        return [json.loads(self.r.hget("quarantine", i)) for i in ids if self.r.hget("quarantine", i)]

    # ---- feedback + quality (L6) ---------------------------------------------------------
    def add_feedback(self, run_id: str, fb: dict) -> None:
        rec = {**fb, "run_id": run_id, "ts": time.time()}
        self.r.rpush("feedback", json.dumps(rec))
        self.r.hincrby("feedback:counts", "satisfactory" if fb["satisfactory"] else "unsatisfactory", 1)
        M.FEEDBACK.labels("satisfactory" if fb["satisfactory"] else "unsatisfactory").inc()

    def feedback_summary(self) -> dict:
        c = self.r.hgetall("feedback:counts")
        sat, unsat = int(c.get("satisfactory", 0)), int(c.get("unsatisfactory", 0))
        items = [json.loads(x) for x in self.r.lrange("feedback", -200, -1)]
        reasons: dict[str, int] = {}
        for it in items:
            if not it["satisfactory"]:
                key = it["reason"].strip().lower()[:60]
                reasons[key] = reasons.get(key, 0) + 1
        total = sat + unsat
        return {"total": total, "satisfactory": sat, "unsatisfactory": unsat,
                "satisfaction_rate": round(sat / total, 3) if total else None,
                "top_unsatisfactory_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:5]}

    def add_quality(self, point: dict) -> None:
        self.r.rpush("quality:points", json.dumps(point))
        self.r.ltrim("quality:points", -2000, -1)

    def quality_points(self) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange("quality:points", 0, -1)]

    # ---- caches -------------------------------------------------------------------------------
    def cache_get(self, cache: str, key: str) -> Any | None:
        if not self.s.cache_enabled:
            return None
        v = self.r.get(f"cache:{cache}:{key}")
        M.CACHE.labels(cache, "hit" if v else "miss").inc()
        return json.loads(v) if v else None

    def cache_set(self, cache: str, key: str, value: Any, ttl: int | None = None) -> None:
        if self.s.cache_enabled:
            self.r.setex(f"cache:{cache}:{key}", ttl or self.s.cache_ttl_s, json.dumps(value))

    # ---- rate limit (token bucket, atomic in Lua) ---------------------------------------------------
    _BUCKET = """
    local k=KEYS[1]; local rate=tonumber(ARGV[1]); local cap=tonumber(ARGV[2]); local now=tonumber(ARGV[3])
    local d=redis.call('HMGET',k,'tokens','ts'); local tokens=tonumber(d[1]); local ts=tonumber(d[2])
    if tokens==nil then tokens=cap; ts=now end
    tokens=math.min(cap, tokens+(now-ts)*rate)
    local ok=0; if tokens>=1 then tokens=tokens-1; ok=1 end
    redis.call('HMSET',k,'tokens',tokens,'ts',now); redis.call('EXPIRE',k,120)
    return {ok, tostring(tokens)}
    """

    def allow(self, client_id: str) -> tuple[bool, float]:
        if not hasattr(self, "_bucket_sha"):
            self._bucket_sha = self.r.script_load(self._BUCKET)
        rate = self.s.rate_limit_per_min / 60.0
        ok, tokens = self.r.evalsha(self._bucket_sha, 1, f"rl:{client_id}", rate, self.s.rate_limit_burst, time.time())
        return bool(ok), float(tokens)

    # ---- streams ----------------------------------------------------------------------------------------
    def ensure_groups(self) -> None:
        for stream, group in ((STREAM_INTAKE, GROUP_INTAKE), (STREAM_COVERAGE, GROUP_COVERAGE)):
            try:
                self.r.xgroup_create(stream, group, id="0", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise

    def enqueue(self, stream: str, run_id: str, carrier: dict | None = None) -> str:
        return self.r.xadd(stream, {"run_id": run_id, "enqueued_at": time.time(), "otel": json.dumps(carrier or {})},
                           maxlen=100000, approximate=True)

    def queue_depth(self) -> dict:
        out = {}
        for stream, group in ((STREAM_INTAKE, GROUP_INTAKE), (STREAM_COVERAGE, GROUP_COVERAGE)):
            try:
                info = [g for g in self.r.xinfo_groups(stream) if g["name"] == group]
                lag = (info[0].get("lag") or 0) if info else 0
                pending = info[0]["pending"] if info else 0
                out[stream] = int(lag) + int(pending)
            except redis.ResponseError:
                out[stream] = 0
        return out


def _delay_days(claim: dict) -> int:
    rep = date.fromisoformat(claim["date_reported"]) if claim.get("date_reported") else date.today()
    return (rep - date.fromisoformat(claim["date_of_loss"])).days


def claim_fingerprint(claim: dict, prompt_versions: dict, index_version: str) -> str:
    """Whole-run cache key: identical narrative + facts that affect the answer + prompt/index versions."""
    keyed = {
        "d": " ".join(claim["claim_description"].lower().split()), "x": " ".join(claim["claimed_damage"].lower().split()),
        "t": claim["claim_type_declared"], "vc": claim["vehicle"]["vehicle_class"], "c": claim.get("estimated_repair_cost_inr"),
        "dr": claim["vehicle"]["drivable"], "tp": claim["third_party_involved"], "inj": claim["injuries_reported"],
        "pf": claim["police_report"]["filed"], "own": claim["driver"]["is_owner"], "lic": claim["driver"].get("licence_valid"),
        "idv": claim["vehicle"]["idv_inr"], "pv": prompt_versions, "iv": index_version,
        "late": _delay_days(claim) > get_settings().late_report_days,
        "early": (date.fromisoformat(claim["date_of_loss"]) - date.fromisoformat(claim["policy_start_date"])).days <= 30,
    }
    return hashlib.sha256(json.dumps(keyed, sort_keys=True, default=str).encode()).hexdigest()[:24]
