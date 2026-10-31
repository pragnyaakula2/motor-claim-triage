"""L5 + D7 measurement: cost per 100 runs by step, and the effect of caching on p95 and cost.

Needs a running stack (scripts/devstack.sh start 3) with LLM_PRICE_* set. Uses ~12 distinct answerable scenarios.
  A  run cache bypassed (retrieval/embedding caches still warm after first pass)   -> baseline p95 + cost
  B  same payloads, run cache populated by a verified first pass, then replayed     -> cached p95 + cost
"""
import concurrent.futures as cf, json, statistics as st, sys, time, httpx
sys.path.insert(0, ".")
from evaluation.run_eval import load_scenarios, payload_for

BASE = "http://localhost:8000"
H = httpx.Client(base_url=BASE, timeout=60)
SC = [s for s in load_scenarios(None) if s["expected"]["outcome"] == "answer"][:12]


def one(payload, bypass):
    t0 = time.time()
    r = H.post("/v1/claims", json=payload, headers={"X-Client-Id": "cost", **({"X-Bypass-Cache": "1"} if bypass else {})})
    rid = r.json()["run_id"]
    while True:
        s = H.get(f"/v1/runs/{rid}").json()
        if s["status"] in ("completed", "escalated", "failed"):
            return rid, time.time() - t0, s["result"], r.json().get("cached")
        time.sleep(0.1)


def batch(n, bypass):
    pl = [payload_for(SC[i % len(SC)]) for i in range(n)]
    with cf.ThreadPoolExecutor(6) as ex:
        return list(ex.map(lambda p: one(p, bypass), pl))


def p95(xs):
    xs = sorted(xs)
    return xs[int(0.95 * (len(xs) - 1))]


def summarize(name, res):
    lat = [r[1] for r in res]
    cost = [r[2].get("cost_usd", 0) for r in res]
    return {"phase": name, "runs": len(res), "p50_s": round(st.median(lat), 2), "p95_s": round(p95(lat), 2),
            "cost_per_100_runs_usd": round(100 * sum(cost) / len(cost), 5), "cache_hits": sum(1 for r in res if r[3])}


out = {}
batch(12, True)                      # warm retrieval/embedding caches (not the run cache)
A = batch(60, True)
out["A_run_cache_bypassed"] = summarize("A", A)
# step attribution from traces of phase A
steps, tool_hit, tool_miss = {}, [], []
for rid, *_ in A:
    t = H.get(f"/v1/runs/{rid}/trace").json()
    for k, v in t["totals"]["by_step"].items():
        base = k.split("#")[0]
        d = steps.setdefault(base, {"cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        for f in d:
            d[f] += v[f]
    for e in t["events"]:
        if e["type"] == "tool_call" and e["tool"] == "search_policy":
            (tool_hit if e.get("cache") == "hit" else tool_miss).append(e["latency_s"])
n = len(A)
out["cost_by_step_per_100_runs"] = {k: {"cost_usd": round(100 * v["cost_usd"] / n, 5), "prompt_tokens": round(100 * v["prompt_tokens"] / n),
                                        "completion_tokens": round(100 * v["completion_tokens"] / n), "calls": round(100 * v["calls"] / n)} for k, v in steps.items()}
out["largest_cost_driver"] = max(steps, key=lambda k: steps[k]["cost_usd"])
out["retrieval_cache"] = {"search_hit_mean_ms": round(1000 * st.mean(tool_hit), 2) if tool_hit else None, "search_miss_mean_ms": round(1000 * st.mean(tool_miss), 2) if tool_miss else None,
                          "hits": len(tool_hit), "misses": len(tool_miss)}
batch(12, False)                     # populate run cache with verified answers
B = batch(60, False)
out["B_run_cache_enabled_replay"] = summarize("B", B)
print(json.dumps(out, indent=1))
open("loadtest/results/cost_cache_report.json", "w").write(json.dumps(out, indent=1))
