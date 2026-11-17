"""Gate publication on successful same-revision Windows and GPU artifacts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sha = os.environ['GITHUB_SHA']
repo = os.environ['GITHUB_REPOSITORY']
contract = {'id': 'concat-cuda-hybrid-v1', 'version': 1}
windows = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
assert windows['source_sha'] == sha and windows['render_contract'] == contract
assert windows['smoke'] == 'passed'

def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)

for attempt in range(120):
    data = json.loads(gh('api', f'repos/{repo}/actions/workflows/concat-salad-candidate.yml/runs?head_sha={sha}&per_page=100'))
    candidates = [r for r in data['workflow_runs'] if r['head_sha'] == sha and r['head_branch'] == 'main' and r['event'] in ('push', 'workflow_dispatch')]
    if candidates:
        run = max(candidates, key=lambda r: r['id'])
        if run['status'] == 'completed':
            assert run['conclusion'] == 'success', f'Matching GPU candidate failed: {run["html_url"]}'
            break
    time.sleep(15)
else:
    raise RuntimeError('Matching GPU candidate did not complete within 30 minutes')
with tempfile.TemporaryDirectory() as work:
    gh('run', 'download', str(run['id']), '--repo', repo, '--name', 'Concat-Salad-image-reference', '--dir', work)
    image = json.loads((Path(work) / 'image-reference.json').read_text())
assert image['source_sha'] == sha and image['render_contract'] == contract
assert image['image'] == f'ghcr.io/{repo.lower()}-gpu-candidate:{sha}'
assert '@sha256:' in image['digest']
assert image['worker_startup'] == image['authenticated_capabilities'] == 'passed'
Path('matching-release.json').write_text(json.dumps({'source_sha': sha, 'windows': windows, 'gpu': image, 'gpu_workflow': run['html_url']}, indent=2))
print(f'Matched Windows EXE and GPU candidate at main {sha}')
