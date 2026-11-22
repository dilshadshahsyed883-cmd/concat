"""Actual Docker ENTRYPOINT health/auth/capabilities smoke on loopback only."""
import json
import os
import time
import urllib.error
import urllib.request

base = 'http://127.0.0.1:18080'
def get(path, authenticated=False):
    headers = {'Authorization': 'Bearer ' + os.environ['SMOKE_TOKEN']} if authenticated else {}
    request = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())

for attempt in range(40):
    try:
        code, result = get('/health/live')
        if code == 200 and result.get('ok') is True:
            break
    except (OSError, ValueError):
        pass
    time.sleep(2)
else:
    raise RuntimeError('Built container ENTRYPOINT did not become live')
assert get('/api/capabilities')[0] == 401
code, capabilities = get('/api/capabilities', True)
assert code == 200
assert capabilities['source_sha'] == os.environ['GITHUB_SHA']
assert capabilities['render_contract'] == {'id': 'concat-cuda-hybrid-v1', 'version': 2}
code, readiness = get('/health/ready')
assert code in (200, 503) and readiness['cuda_stage1'] is True and readiness['native_vulkan'] is False
assert get('/api/config/concurrency', True)[0] == 200
print('Actual Docker ENTRYPOINT, liveness, authentication and exact source/contract PASS; physical GPU unverified')
