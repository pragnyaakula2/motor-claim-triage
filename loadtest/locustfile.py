"""Load test (D6/A7). Measures a run from submission to final result, not the submission call alone.

    locust -f loadtest/locustfile.py --headless -u 5 -r 5 -t 3m --host http://localhost:8000 --csv loadtest/results/run
Each user submits 1 run/s (open loop); polling happens in a spawned greenlet so a slow run does not slow the arrival rate.
Custom stat 'RUN e2e' = POST sent -> terminal status observed. Failures: non-202, no terminal status in 120 s, AGENT_FAILURE/RUN_TIMEOUT.
"""
import random
import time

import gevent
from locust import HttpUser, constant_throughput, events, task

from claimtriage.samples import base_fnol, food_delivery_fnol

TERMINAL = {"completed", "escalated", "failed"}
INFRA_CODES = {"AGENT_FAILURE", "RUN_TIMEOUT"}


class Claimant(HttpUser):
    wait_time = constant_throughput(1)

    def on_start(self):
        self.cid = f"load-{random.randint(0, 10**9)}"

    @task
    def submit_run(self):
        payload = food_delivery_fnol() if random.random() < 0.2 else base_fnol()
        t0 = time.time()
        with self.client.post("/v1/claims", json=payload, name="POST /v1/claims (submit only)",
                              headers={"X-Bypass-Cache": "1", "X-Client-Id": self.cid}, catch_response=True) as r:
            if r.status_code != 202:
                r.failure(f"status {r.status_code}")
                return
            run_id = r.json()["run_id"]
        gevent.spawn(self._poll, run_id, t0)

    def _poll(self, run_id, t0):
        delay = 0.3
        while time.time() - t0 < 120:
            r = self.client.get(f"/v1/runs/{run_id}", name="GET /v1/runs/{id} (poll)")
            if r.status_code == 200 and r.json()["status"] in TERMINAL:
                res = r.json().get("result") or {}
                code = (res.get("escalation") or {}).get("reason_code")
                bad = r.json()["status"] == "failed" or code in INFRA_CODES
                events.request.fire(request_type="RUN", name="e2e", response_time=(time.time() - t0) * 1000, response_length=0,
                                    exception=Exception(f"infra:{code}") if bad else None, context={})
                return
            gevent.sleep(delay)
            delay = min(delay * 1.15, 1.0)
        events.request.fire(request_type="RUN", name="e2e", response_time=120000, response_length=0, exception=Exception("timeout"), context={})
