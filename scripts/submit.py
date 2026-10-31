"""Submit one sample FNOL and wait for the result:  python scripts/submit.py [food|normal|file.json]"""
import json, os, sys, time, httpx
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from claimtriage.samples import base_fnol, food_delivery_fnol
BASE = "http://localhost:8000"
which = sys.argv[1] if len(sys.argv) > 1 else "food"
payload = food_delivery_fnol() if which == "food" else base_fnol() if which == "normal" else json.load(open(which))
t0 = time.time()
r = httpx.post(f"{BASE}/v1/claims", json=payload, timeout=10, headers={"X-Bypass-Cache": "1"})  # always a fresh run, so injected faults are visible
print(r.status_code, json.dumps(r.json())[:400])
if r.status_code != 202:
    sys.exit(1)
rid = r.json()["run_id"]
while True:
    s = httpx.get(f"{BASE}/v1/runs/{rid}").json()
    if s["status"] in ("completed", "escalated", "failed"):
        break
    time.sleep(0.3)
print(f"\nstatus={s['status']} in {time.time()-t0:.1f}s")
print(json.dumps(s["result"], indent=1)[:4500])
