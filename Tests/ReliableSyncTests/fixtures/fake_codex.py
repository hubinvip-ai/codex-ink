#!/usr/bin/env python3
"""Read-only JSONL contract fixture; never contacts an external service."""
import json
import sys

responses = {
    'initialize': {},
    'thread/list': {'data': []},
    'account/rateLimits/read': {'rateLimits': {'primary': {'usedPercent':45, 'resetsAt':1788771680}}},
    'account/usage/read': {'summary': {'lifetimeTokens':50}, 'dailyUsageBuckets':[{'startDate':'2026-09-01', 'tokens':50}]},
    'account/read': {'account': {'type':'chatgpt', 'email':'example@example.test', 'planType':'plus'}},
}
for line in sys.stdin:
    request = json.loads(line)
    if 'id' in request:
        payload = {'id':request['id'], 'result':responses[request['method']]}
        print(json.dumps(payload), flush=True)
