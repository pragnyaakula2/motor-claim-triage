"""Summarise a Locust --csv run against the declared SLO:  python loadtest/summarize.py loadtest/results/<prefix>"""
import csv, json, sys
SLO = {"rps": 5.0, "p95_ms": 8000, "fail_pct": 1.0}
rows = {r["Name"]: r for r in csv.DictReader(open(sys.argv[1] + "_stats.csv"))}
e = rows.get("e2e")
sub = rows.get("POST /v1/claims (submit only)")
if not e:
    sys.exit("no e2e rows")
n, f = int(e["Request Count"]), int(e["Failure Count"])
dur = float(sys.argv[2]) if len(sys.argv) > 2 else None
out = {"runs_completed": n, "infra_failures": f, "failure_pct": round(100 * f / max(1, n + f), 2),
       "e2e_p50_ms": float(e["50%"]), "e2e_p95_ms": float(e["95%"]), "e2e_p99_ms": float(e["99%"]), "e2e_max_ms": float(e["Max Response Time"]),
       "completed_runs_per_s": float(e["Requests/s"]),
       "submit_p95_ms": float(sub["95%"]) if sub else None, "submit_failures": int(sub["Failure Count"]) if sub else None}
out["meets_p95"] = out["e2e_p95_ms"] <= SLO["p95_ms"]
out["meets_failure_rate"] = out["failure_pct"] <= SLO["fail_pct"]
out["meets_rps"] = out["completed_runs_per_s"] >= SLO["rps"] * 0.95
out["SLO_MET"] = out["meets_p95"] and out["meets_failure_rate"] and out["meets_rps"]
print(json.dumps(out, indent=1))
