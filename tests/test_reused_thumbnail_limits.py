"""Exercise thumbnail boundaries without Drive or YouTube calls."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOADER = ROOT / 'reused/uploader/part01'


class ThumbnailLimitTests(unittest.TestCase):
    def test_concat_backend_preflight_and_rendered_limit(self):
        script = r'''
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote_plus as unquote
from fastapi.testclient import TestClient
from desktop_core.modules.thumbnail_limits import MAX_THUMBNAIL_BYTES as LIMIT
from desktop_core.modules.preflight import validate_job
from lum_engine import standalone_app as web

assert LIMIT == 50 * 1024 * 1024

def png(size):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    header = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 120, 68, 8, 2, 0, 0, 0))
    image = chunk(b'IDAT', zlib.compress((b'\x00' + b'\x00' * 360) * 68))
    end = chunk(b'IEND', b'')
    return header + chunk(b'tEXt', b'note\x00' + b'x' * (size - len(header + image + end) - 17)) + image + end

staging = web.STAGING_ROOT / 'verified.mp4'
staging.parent.mkdir(parents=True, exist_ok=True)
staging.write_bytes(b'verified video fixture')
client = TestClient(web.app, headers={'X-Auth-User': 'alice', 'X-App-Access': 'all'})
user = {'username': 'alice', 'is_admin': False}
with patch.object(web, 'current_user', return_value=user), \
     patch.object(web, 'require_csrf'), \
     patch.object(web, 'studio_tenants'), \
     patch.object(web.db, 'channel_group_names_map', return_value={1: 'Tennis'}), \
     patch.object(web, 'stage_concat_link', return_value=(str(staging), 'verified.mp4', 22, 'sha')), \
     patch.object(web.store, 'create_project', return_value=123) as create, \
     patch.object(web.store, 'update'):
    def post(data, name='thumb.png'):
        return client.post('/concat-studio/concat-uploader/actions/add-video',
            data={'csrf_token': 'test', 'title': 'Boundary', 'channel_db_id': 1,
                  'group_name': 'Tennis', 'source_mode': 'concat_link',
                  'concat_video_link': 'https://concat.scenebuilder.online/api/cloud-render/media/fixture/file'},
            files={'thumbnail': (name, data, 'image/png')}, follow_redirects=False)

    for size in (2 * 1024 * 1024, 2 * 1024 * 1024 + 1, LIMIT, LIMIT + 1):
        data = png(size)
        assert len(data) == size
        before = create.call_count
        response = post(data)
        location = unquote(response.headers['location'])
        assert response.status_code == 303
        if size <= LIMIT:
            assert create.call_count == before + 1, location
            path = Path(web.THUMBNAIL_ROOT) / '123.png'
            assert path.stat().st_size == size
            assert not any(x.code == 'THUMBNAIL_LARGE' for x in validate_job({'thumbnail_path': str(path)}))
        else:
            assert create.call_count == before and '50 MB' in location, location
            with (Path(web.THUMBNAIL_ROOT) / 'large.png').open('wb') as stream:
                stream.truncate(size)
            assert any(x.code == 'THUMBNAIL_LARGE' for x in validate_job({'thumbnail_path': str(Path(web.THUMBNAIL_ROOT) / 'large.png')}))
        del data

    # Shared validation keeps the previous cap for other uploader modes.
    import os
    with patch.dict(os.environ, {'LUM_ENGINE_INSTANCE': 'engine1'}):
        before = create.call_count
        response = post(png(2 * 1024 * 1024 + 1))
        assert create.call_count == before and '2 MB' in unquote(response.headers['location'])
        assert any(x.code == 'THUMBNAIL_LARGE' for x in validate_job({'thumbnail_path': str(Path(web.THUMBNAIL_ROOT) / '123.png')}))

    for data, name, error in ((b'', 'thumb.png', 'empty'), (b'bad', 'thumb.gif', 'JPG or PNG')):
        before = create.call_count
        response = post(data, name)
        assert create.call_count == before
        assert error in unquote(response.headers['location'])

# The exact same constant reaches each form's browser validation.
with patch.object(web, 'health', return_value={}), patch.object(web, 'stats_payload', return_value={}):
    # Template rendering uses the actual handler context; existing UI tests cover the full page.
    template = web.templates.get_template('app.html')
    for mode, input_id in (('concat', 'concat-thumbnail'),):
        html = template.render(request=MagicMock(), user={'is_admin': False}, engine_instance=mode,
            tab='add_video', max_thumbnail_bytes=LIMIT, base_path='/concat-studio/concat-uploader', stats={},
            health={}, ae={}, mode='simple')
        assert f'id="{input_id}" data-max-thumbnail-bytes="{LIMIT}"' in html
        assert 'maximum 50 MB' in html
print('PASS Concat thumbnail admission, preflight and rendered limit')
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(UPLOADER),
                       LUM_ENGINE_INSTANCE='concat', LUM_DATA_DIR=directory,
                       CONCAT_CLOUD_RENDER_ROOT=str(Path(directory) / 'cloud'),
                       LUM_BASE_PATH='/concat-studio/concat-uploader', LUM_SESSION_SECRET='thumbnail-test-secret',
                       LUM_SUITE_AUTH='1', LUM_AUTO_ENGINE_V6_ROOT=str(Path(directory) / 'auto_engine_v6'))
            for stale in ('YOUTUBE_UPLOAD_MANAGER_DATA_DIR', 'LUM_GOOGLE_OAUTH_ROOT'):
                env.pop(stale, None)
            done = subprocess.run([sys.executable, '-c', script], env=env, cwd=ROOT,
                                  capture_output=True, text=True, timeout=90)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
