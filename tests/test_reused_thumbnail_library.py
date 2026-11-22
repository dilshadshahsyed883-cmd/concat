"""Real owner HTTP/filesystem/queue and thumbnail-only worker recovery regressions."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if os.getenv('CONCAT_THUMBNAIL_TEST_CHILD') != '1':
    class IsolatedThumbnailTests(unittest.TestCase):
        def test_library_regressions(self):
            with tempfile.TemporaryDirectory() as root:
                env = os.environ.copy()
                for key in ('YOUTUBE_UPLOAD_MANAGER_DATA_DIR', 'LUM_GOOGLE_OAUTH_ROOT', 'LUM_AUTO_ENGINE_V6_ROOT'):
                    env.pop(key, None)
                env.update(CONCAT_THUMBNAIL_TEST_CHILD='1', CONCAT_METADATA_TEST_CHILD='1', LUM_ENGINE_INSTANCE='concat',
                           LUM_DATA_DIR=str(Path(root) / 'concat-thumbnails'), LUM_BASE_PATH='/concat-uploader', LUM_SUITE_AUTH='1',
                           CONCAT_WEB_IN_PROCESS='1', LUM_COOKIE_SECURE='1', LUM_SESSION_SECRET='thumbnail-test-session-secret-over-thirty-two',
                           PYTHONPATH=str(ROOT / 'reused/uploader/part01'))
                result = subprocess.run([sys.executable, __file__, '-v'], env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stderr)
else:
    import hashlib
    import io
    import sqlite3
    import threading
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock, patch
    from PIL import Image
    from test_reused_concat_metadata import MetadataTests as BaseMetadataTests, LINK, SYNC
    from lum_engine import standalone_app as web
    from lum_engine.config import STAGING_ROOT, THUMBNAIL_ROOT
    from lum_engine.project_store import ProjectStore
    from lum_engine.thumbnail_library import ThumbnailLibrary
    from lum_engine.worker import VPSUploadWorker

    URL = '/concat-uploader/api/concat/thumbnails'
    def image_bytes(fmt='PNG', color='red'):
        stream = io.BytesIO()
        Image.new('RGB', (120, 68), color).save(stream, format=fmt)
        return stream.getvalue()

    class ThumbnailTests(BaseMetadataTests):
        def setUp(self):
            super().setUp()
            self.library = ThumbnailLibrary(self.store)
            self.store.set_setting('delete_staged_after_verified', '0')
            self.worker = VPSUploadWorker.__new__(VPSUploadWorker)
            self.worker.store = self.store
            self.worker.db = Mock()
            self.worker.auth = Mock()
            self.worker.uploader = Mock()
            self.worker.uploader.process_queue.side_effect = AssertionError('cleanup attempted video upload')
            self.worker.shutdown_event = threading.Event()

        def add_thumb(self, name='u1.png', payload=None, replace=False):
            response = self.client.post(URL + '/add', data={'csrf_token': self.csrf, 'replace_existing': '1' if replace else ''},
                         files={'thumbnail': (name, payload if payload is not None else image_bytes(), 'image/png')})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['id']

        def make_project(self, identity, job='selected-video'):
            data = self.content if job == 'selected-video' else self.content + job.encode()
            sha = hashlib.sha256(data).hexdigest()
            if job != 'selected-video':
                (self.media / (job + '.mp4')).write_bytes(data)
                with sqlite3.connect(self.db_path) as con:
                    con.execute('INSERT INTO files VALUES(?,?,?,?,?)', (job, job + '.mp4', len(data), sha, 'ready'))
            STAGING_ROOT.mkdir(exist_ok=True)
            stage = STAGING_ROOT / (self.root.name + '-' + job + '.mp4')
            stage.write_bytes(data)
            thumb, thumb_sha = self.library.stage('alice', identity)
            return self.store.create_project(dict(owner_username='alice', channel_db_id=1, title='Library test', source_mode='local_file',
                source_origin='concat_link', concat_job_id=job, staging_path=str(stage), source_fingerprint=sha,
                thumbnail_path=thumb, library_thumbnail_id=identity, library_thumbnail_sha=thumb_sha))

        def verify(self, pid, state='SUCCEEDED'):
            self.store.update(pid, remote_verified_at='2026-11-22T00:00:00+00:00', youtube_video_id='remote', core_video_id=pid)
            self.worker.db.get_video.return_value = dict(youtube_video_id='remote', thumbnail_stage_state=state, channel_token_file_path='owner-token')

        def finish(self, pid):
            self.worker._process(pid)

        def due(self, pid):
            self.store.update(pid, concat_cleanup_retry_at='')

        def test_image_validation_duplicate_replace_and_damaged_files(self):
            identity = self.add_thumb()
            response = self.client.post(URL + '/add', data={'csrf_token': self.csrf}, files={'thumbnail': ('u1.png', image_bytes(), 'image/png')})
            self.assertEqual(response.status_code, 400)
            replacement = self.add_thumb(payload=image_bytes(color='blue'), replace=True)
            self.assertNotEqual(identity, replacement)
            self.assertEqual([r['id'] for r in self.library.items('alice')], [replacement])
            for name, data in (('../bad.png', image_bytes()), ('bad.gif', image_bytes()), ('bad.png', b'bad'),
                               ('wrong.jpg', image_bytes()), ('empty.png', b''), ('damaged.png', image_bytes()[:-12])):
                response = self.client.post(URL + '/add', data={'csrf_token': self.csrf}, files={'thumbnail': (name, data, 'image/png')})
                self.assertEqual(response.status_code, 400, name)
            with patch('lum_engine.thumbnail_library.MAX_THUMBNAIL_BYTES', 10):
                with self.assertRaises(ValueError):
                    self.library.add('alice', 'too-big.png', image_bytes())

        def test_preview_owner_isolation_csrf_and_symlinks(self):
            identity = self.add_thumb()
            path = URL + '/' + identity + '/file'
            preview = self.client.get(path)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.content, image_bytes())
            self.assertIn('no-store', preview.headers['cache-control'])
            self.owner = 'bob'
            self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.get(URL).json()['items'], [])
            self.assertEqual(self.client.post(URL + '/delete', data={'csrf_token': self.csrf, 'ids': identity}).json()['deleted'], [identity])
            self.owner = 'alice'
            self.assertEqual(len(self.library.items('alice')), 1)
            self.assertEqual(self.client.post(URL + '/delete', data={'csrf_token': 'bad', 'ids': identity}).status_code, 403)
            self.assertEqual(self.client.post(URL + '/add', data={'csrf_token': 'bad'}, files={'thumbnail': ('x.png', image_bytes(), 'image/png')}).status_code, 403)
            with self.store.connect() as conn:
                row = self.library.row(conn, 'alice', identity)
            file = self.library.path(row, 'alice')
            file.unlink(); file.symlink_to(self.media / 'selected-video.mp4')
            self.assertEqual(self.client.get(path).status_code, 404)
            with self.assertRaises(OSError):
                self.library.delete('alice', identity)
            self.assertTrue((self.media / 'selected-video.mp4').exists())

        def test_metadata_matches_unique_filename_and_flags_ambiguity(self):
            identity = self.add_thumb()
            self.save()
            rows = [{'video_name': 'u1', 'title': 'Title', 'description': 'Description'}]
            def sync():
                return self.client.post(SYNC, headers={'X-Concat-Thumbnails': '1'}, data={'csrf_token': self.csrf, 'concat_video_link': LINK})
            with patch.object(web, 'tenant_fetch_sheet_csv', return_value=rows):
                response = sync()
                self.assertEqual(response.json()['thumbnail']['id'], identity)
                self.add_thumb('u1.jpg', image_bytes('JPEG'))
                response = sync()
                self.assertIsNone(response.json()['thumbnail'])
                self.assertIn('Multiple', response.json()['thumbnail_message'])
                self.assertEqual(response.json()['title'], 'Title')

        def test_selected_and_bulk_delete_protect_queued_failed_and_replace(self):
            identity = self.add_thumb()
            other = self.add_thumb('u2.png')
            pid = self.make_project(identity)
            self.assertTrue(self.library.items('alice')[1]['in_use'])
            response = self.client.post(URL + '/delete', data={'csrf_token': self.csrf, 'ids': [identity, other]})
            self.assertEqual(response.json()['deleted'], [other])
            self.assertEqual(len(response.json()['errors']), 1)
            self.store.update(pid, status='FAILED')
            with self.assertRaises(ValueError):
                self.library.delete('alice', identity)
            with self.assertRaises(ValueError):
                self.library.add('alice', 'u1.png', image_bytes(), replace=True)
            self.store.update(pid, status='CANCELLED')
            self.assertTrue(self.library.delete('alice', identity))

        def test_admission_race_does_not_bind_deleted_or_replaced_thumbnail(self):
            identity = self.add_thumb()
            stage, sha = self.library.stage('alice', identity)
            self.library.delete('alice', identity)
            video = STAGING_ROOT / (self.root.name + '.mp4'); video.write_bytes(self.content)
            with self.assertRaisesRegex(ValueError, 'changed or was deleted'):
                self.store.create_project(dict(owner_username='alice', channel_db_id=1, title='Race', source_mode='local_file',
                    source_origin='concat_link', concat_job_id='selected-video', staging_path=str(video), source_fingerprint=self.sha,
                    thumbnail_path=stage, library_thumbnail_id=identity, library_thumbnail_sha=sha))
            self.assertEqual(self.store.list(), [])

        def test_concurrent_same_name_add_admits_one_and_restart_retains_library(self):
            barrier = threading.Barrier(4)
            def add(_):
                barrier.wait()
                try: return self.library.add('alice', 'same.png', image_bytes())
                except ValueError: return None
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(add, range(4)))
            self.assertEqual(sum(r is not None for r in results), 1)
            restarted = ThumbnailLibrary(ProjectStore(self.store.path))
            self.assertEqual(len(restarted.items('alice')), 1)

        def test_verified_success_deletes_library_and_private_thumbnail_preserves_receipts(self):
            identity = self.add_thumb(); pid = self.make_project(identity)
            self.verify(pid)
            thumb = Path(self.store.get(pid)['thumbnail_path'])
            self.finish(pid)
            self.assertEqual(self.library.items('alice'), [])
            self.assertFalse(thumb.exists())
            self.assertTrue(self.library.use(pid)['applied_at'])
            self.assertEqual(self.store.get(pid)['youtube_video_id'], 'remote')
            self.assertEqual(self.store.get(pid)['status'], 'COMPLETED')
            self.finish(pid)
            self.worker.uploader.process_queue.assert_not_called()
            self.worker.auth.build_youtube_service.assert_not_called()

        def test_unverified_never_deletes_and_failed_thumbnail_retries_only_thumbnail(self):
            identity = self.add_thumb(); pid = self.make_project(identity)
            with self.assertRaisesRegex(RuntimeError, 'independent YouTube'):
                self.worker._finish_verified(pid, self.store.get(pid), Path(self.store.get(pid)['staging_path']))
            self.verify(pid, state='WARNING')
            apply = self.worker.auth.build_youtube_service.return_value.thumbnails.return_value.set.return_value.execute
            apply.side_effect = RuntimeError('thumbnail API denied')
            with self.assertRaisesRegex(RuntimeError, 'thumbnail API denied'):
                self.finish(pid)
            self.assertEqual(len(self.library.items('alice')), 1)
            self.assertTrue((self.media / 'selected-video.mp4').exists())
            self.assertEqual(self.library.use(pid)['applied_at'], '')
            self.assertEqual(self.store.get(pid)['concat_cleanup_pending'], 1)
            apply.side_effect = None; apply.return_value = {'items': [{'url': 'remote-thumbnail'}]}
            self.due(pid); self.worker._retry_concat_cleanup()
            self.assertEqual(self.library.items('alice'), [])
            self.worker.uploader.process_queue.assert_not_called()
            self.worker.db.add_video.assert_not_called()
            self.assertEqual(apply.call_count, 2)

        def test_cleanup_failure_retries_deletion_without_thumbnail_or_video_reupload(self):
            identity = self.add_thumb(); pid = self.make_project(identity); self.verify(pid)
            with patch.object(ThumbnailLibrary, 'delete', side_effect=OSError('cannot unlink')):
                with self.assertRaises(OSError): self.finish(pid)
            self.assertEqual(len(self.library.items('alice')), 1)
            self.assertTrue(self.library.use(pid)['applied_at'])
            self.due(pid); self.worker._retry_concat_cleanup()
            self.assertEqual(self.library.items('alice'), [])
            self.worker.uploader.process_queue.assert_not_called()
            self.worker.auth.build_youtube_service.assert_not_called()

        def test_other_queue_use_defers_then_eventually_deletes_original(self):
            identity = self.add_thumb()
            first = self.make_project(identity); other = self.make_project(identity, 'another-video')
            self.verify(first); self.finish(first)
            self.assertEqual(len(self.library.items('alice')), 1)
            self.assertEqual(self.store.get(first)['status'], 'COMPLETED')
            self.assertIsNone(self.store.get(first)['cleanup_verified_at'])
            self.verify(other); self.finish(other)
            self.assertEqual(self.library.items('alice'), [])
            self.due(first); self.worker._retry_concat_cleanup()
            self.assertEqual(self.store.get(first)['concat_cleanup_pending'], 0)

        def test_large_video_handoff_does_not_block_other_requests(self):
            import asyncio
            import httpx
            from test_reused_concat_metadata import ORIGIN
            channel = web.studio_tenants.create_channel('alice', 'UC' + uuid.uuid4().hex[:22], 'Responsive channel')
            group = 'Responsive ' + self.root.name
            web.studio_tenants.create_group('alice', group, [channel])
            data = dict(csrf_token=self.csrf, concat_video_link=LINK, title='Responsive', source_mode='concat_link',
                        channel_db_id=channel, group_name=group)
            entered, release = threading.Event(), threading.Event()
            original = web.stage_concat_link
            def slow_handoff(*args, **kwargs):
                entered.set()
                release.wait(5)
                return original(*args, **kwargs)
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN,
                                             cookies=self.client.cookies) as client:
                    task = asyncio.create_task(client.post('/concat-uploader/actions/add-video', data=data,
                                                           headers={'Accept': 'application/json'}))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                        self.assertFalse(task.done(), 'handoff must run off the request event loop')
                        response = await asyncio.wait_for(client.get(URL), timeout=1)
                        self.assertEqual(response.status_code, 200)
                        self.assertFalse(task.done(), 'library stays responsive while video handoff is pending')
                    finally:
                        release.set()
                    response = await task
                    self.assertEqual(response.status_code, 200, response.text)
            with patch.object(web, 'stage_concat_link', side_effect=slow_handoff):
                asyncio.run(check())

        def test_json_queue_admission_links_private_copy_and_duplicate_submission_rejected(self):
            identity = self.add_thumb()
            channel = web.studio_tenants.create_channel('alice', 'UC' + uuid.uuid4().hex[:22], 'Library channel')
            group = 'Library ' + self.root.name
            web.studio_tenants.create_group('alice', group, [channel])
            data = dict(csrf_token=self.csrf, concat_video_link=LINK, title='Queued', source_mode='concat_link',
                        channel_db_id=channel, group_name=group, library_thumbnail_id=identity)
            response = self.client.post('/concat-uploader/actions/add-video', data=data, headers={'Accept': 'application/json'})
            self.assertEqual(response.status_code, 200, response.text)
            pid = response.json()['project_id']
            self.assertEqual(self.library.use(pid)['thumbnail_id'], identity)
            self.assertEqual(Path(self.store.get(pid)['thumbnail_path']).read_bytes(), image_bytes())
            duplicate = self.client.post('/concat-uploader/actions/add-video', data=data, headers={'Accept': 'application/json'})
            self.assertEqual(duplicate.status_code, 400)
            self.assertIn('already', duplicate.json()['detail'])
            self.assertEqual(len(self.store.list()), 1)

if os.getenv('CONCAT_THUMBNAIL_TEST_CHILD') == '1':
    del BaseMetadataTests

if __name__ == '__main__':
    unittest.main()
