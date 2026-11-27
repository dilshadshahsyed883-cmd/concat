"""Sheet removal inventory regression through the real Concat uploader routes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SheetRemovalTests(unittest.TestCase):
    def test_remove_hides_inventory_and_rejects_stale_submissions(self):
        script = r'''
import re
from types import SimpleNamespace
from fastapi.testclient import TestClient
from lum_engine import config
from desktop_core.modules.database import Database
from lum_engine import standalone_app as web
from lum_engine.studio_tenants import StudioTenants

db, tenants = web.db, web.studio_tenants
alice = {"username": "alice", "is_admin": 1}
bob = {"username": "bob", "is_admin": 0}

def source(owner, name):
    sid = db.save_sheet_source(sheet_name=name, sheet_url_or_id=name)
    tenants.bind("sheet", sid, owner)
    return sid

def sync(owner, sid, suffix, group):
    row = SimpleNamespace(key=suffix, channel_id="UC" + suffix * 22,
                          channel_name="Channel " + suffix, gmail="", notes="", group=group)
    tenants.sync_owned_sheet_rows(owner, sid, [row], sync_sheet_groups=True)
    with db.connect() as con:
        return int(con.execute("SELECT id FROM channels WHERE channel_id=?",
                               (row.channel_id,)).fetchone()[0])

s1, s2, foreign = source("alice", "one"), source("alice", "two"), source("bob", "foreign")
c1 = sync("alice", s1, "a", "Exclusive, Shared")
c2 = sync("alice", s2, "b", "Shared")
c3 = sync("bob", foreign, "c", "Foreign")
manual = tenants.create_channel("alice", "UC" + "d" * 22, "Manual survivor")
tenants.create_group("alice", "Manual group", [manual])
with db.connect() as con:
    con.execute("UPDATE channels SET token_file_path='retained-token.json' WHERE id=?", (c1,))
    history = con.execute("INSERT INTO videos(local_video_path,title,channel_db_id,status) VALUES ('old.mp4','Historical',?,'COMPLETED')", (c1,)).lastrowid

client = TestClient(web.app, headers={"X-Auth-User": "alice", "X-Is-Admin": "1", "X-App-Access": "concat"}, follow_redirects=False)
page = client.get('/concat-uploader/app?tab=sheets')
assert page.status_code == 200, page.text
assert '>Remove</button>' in page.text
csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
# Authorization and CSRF must both precede mutation.
reply = client.post('/concat-uploader/actions/sheets/delete', data={"source_id": s1, "csrf_token": "invalid"})
assert reply.status_code == 403, reply.text
assert db.get_sheet_source(s1)
reply = client.post('/concat-uploader/actions/sheets/delete', data={"source_id": foreign, "csrf_token": csrf})
assert 'error=' in reply.headers['location'], reply.text
assert db.get_sheet_source(foreign)
reply = client.post('/concat-uploader/actions/sheets/delete', data={"source_id": s1, "csrf_token": csrf})
assert reply.status_code == 303 and 'error=' not in reply.headers['location'], reply.text
assert tenants.current_channel_ids(alice) == {c2, manual}
assert tenants.current_channel_ids(bob) == {c3}
assert {r['id'] for r in web.all_channels(alice)} == {c2, manual}
for tab in ('channels', 'add_video'):
    page = client.get('/concat-uploader/app?tab=' + tab)
    assert page.status_code == 200, page.text
    assert 'Channel a' not in page.text
    assert 'Channel b' in page.text
    assert 'Manual survivor' in page.text
    assert 'Exclusive' not in page.text
    assert 'Foreign' not in page.text
assert {g['group_name']: g['channel_count'] for g in tenants.current_groups(alice)} == {'Shared': 1, 'Manual group': 1}
reply = client.post('/concat-uploader/actions/add-video', data={"csrf_token": csrf, "title": "Stale", "channel_db_id": c1, "group_name": "Exclusive"})
assert 'no+longer' in reply.headers['location'] or 'no%20longer' in reply.headers['location'], reply.headers
try:
    tenants.create_group('alice', 'Stale group', [c1])
except PermissionError:
    pass
else:
    raise AssertionError('Removed channel accepted for a new group')
assert db.get_channel(c1)['token_file_path'] == 'retained-token.json'
with db.connect() as con:
    assert con.execute('SELECT status FROM videos WHERE id=?', (history,)).fetchone()[0] == 'COMPLETED'
# Reopening persisted state and re-syncing the remaining source cannot restore removed inventory.
restarted = StudioTenants(Database(db.db_path))
sync('alice', s2, 'b', 'Shared')
assert restarted.current_channel_ids(alice) == {c2, manual}
tenants.delete_owned_sheet('alice', s2)
assert restarted.current_channel_ids(alice) == {manual}
assert {g['group_name'] for g in restarted.current_groups(alice)} == {'Manual group'}
assert restarted.current_channel_ids(bob) == {c3}
print('PASS sheet removal routes, inventory, groups, stale uploads, CSRF, isolation, history and restart')
'''
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            for key in ('YOUTUBE_UPLOAD_MANAGER_DATA_DIR', 'LUM_GOOGLE_OAUTH_ROOT', 'LUM_AUTO_ENGINE_V6_ROOT'):
                env.pop(key, None)
            env.update({
                'LUM_ENGINE_INSTANCE': 'concat', 'LUM_DATA_DIR': str(Path(root) / 'uploader'),
                'LUM_BASE_PATH': '/concat-uploader', 'LUM_SUITE_AUTH': '1',
                'CONCAT_WEB_IN_PROCESS': '1',
                'LUM_COOKIE_SECURE': '0',
                'LUM_SESSION_SECRET': 'sheet-removal-test-only-secret-12345',
                'PYTHONPATH': str(ROOT / 'reused/uploader/part01'),
            })
            result = subprocess.run([sys.executable, '-c', script], env=env, cwd=ROOT,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
