#!/usr/bin/env python3
"""Local app-server fixture: public read methods only, no network or credentials."""
import json
import sys
import time
from pathlib import Path

responses = {
    "initialize": {},
    "thread/list": {"data": []},
    "account/rateLimits/read": {
        "rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 90, "resetsAt": int(time.time()) + 3600},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 20, "resetsAt": int(time.time()) + 60000},
        }
    },
    "account/usage/read": {"dailyUsageBuckets": []},
    "account/read": {"account": {"email": "native-fixture@example.test", "planType": "pro"}},
}

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialized":
        continue
    if method not in responses:
        raise RuntimeError("unexpected fixture RPC")
    with Path(__file__).with_suffix(".calls").open("a") as calls:
        calls.write(method + "\n")
    print(json.dumps({"id": request["id"], "result": responses[method]}), flush=True)
