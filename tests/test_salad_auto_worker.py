"""Offline lifecycle, durable mutation journal and queue gate regressions."""
from datetime import datetime, timezone
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from app.modules.concat.engine.cloud_render import CloudRender
from app.modules.concat.engine.salad_worker import SaladAutoWorker
from tests.test_concat_drive_intake import bundle, FakeWorker

ID = 'db3a4591-efc3-46c0-b06a-3d820c0ec100'
MACHINE = 'eb3a4591-efc3-46c0-b06a-3d820c0ec100'


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = CloudRender(Path(self.tmp.name), 'a' * 64)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.manager.pool.shutdown, wait=True)
        self.manager.save({'auto_start': True, 'endpoint': 'https://worker.salad.cloud',
            'worker_key': 'test-worker', 'salad_key': 'test-management',
            'salad_organization': 'org-one', 'salad_project': 'project-one',
            'salad_container_group': 'group-one'})
        self.worker = Mock(timeout=60, health='/health/ready')
        self.manager.production_worker = Mock(return_value=self.worker)
        self.group = {'replicas': 1, 'autostart_policy': False,
                      'current_state': {'status': 'running'}, 'pending_change': False}
        self.instance = {'id': ID, 'machine_id': MACHINE, 'version': 1,
                         'state': 'downloading', 'update_time': self.stamp(1000),
                         'pulling_progress': 0, 'ready': False}
        self.now = 1000
        self.requests = []
        self.post_error = False
        self.patcher = patch('app.modules.concat.engine.salad_worker.requests.request', side_effect=self.api)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    @staticmethod
    def stamp(seconds):
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

    def api(self, method, url, **kw):
        self.requests.append((method, url, kw))
        self.assertFalse(kw['allow_redirects'])
        self.assertEqual(kw['headers']['Salad-Api-Key'], 'test-management')
        if method == 'POST' and self.post_error:
            raise requests.Timeout('ambiguous')
        data = ({'instances': [self.instance]} if url.endswith('/instances') else
                self.instance if '/instances/' in url else self.group)
        return Mock(status_code=202 if method == 'POST' else 200, json=lambda: data)

    def check(self, now=None):
        if now is not None:
            self.now = now
        with patch('app.modules.concat.engine.salad_worker.time.time', return_value=self.now):
            with self.manager.lock, self.manager.connection() as con:
                return self.manager.salad_worker.ensure_ready(con)

    def posts(self):
        return [x[1] for x in self.requests if x[0] == 'POST']

    def status(self):
        return self.manager.config()['salad_worker_status']

    def job(self, state='Queued', index=1):
        with self.manager.connection() as con:
            con.execute('''INSERT INTO production_jobs(remote_id,drive_id,name,resource_keys,
                drive_size,drive_md5,folder_id,drive_path,state)
                VALUES(?,?,?,?,?,?,?,?,?)''', (f'remote-{index}', f'drive-{index}', 'test.zip', '', 1, '', '', '', state))

    def test_full_auto_workflow_with_two_verified_zips_and_output_delivery(self):
        queue = self.manager.production
        source = bundle(Path(self.tmp.name) / 'fixture.zip')
        self.job(index=1)
        self.job(index=2)
        with self.manager.connection() as con:
            con.execute("UPDATE production_jobs SET drive_size=0")
        def download(row, target):
            target.write_bytes(source.read_bytes())
            with zipfile.ZipFile(target, 'a') as archive:
                archive.comment = row['drive_id'].encode()
            return 'fixture.zip'
        queue.drive.download = download
        fake = FakeWorker()
        queue.worker_factory = lambda _: fake
        queue.control('start')
        self.group['current_state']['status'] = 'stopped'
        with patch('app.modules.concat.engine.salad_worker.time.time', return_value=1000) as clock:
            queue.tick_intake()
            queue.tick()
            self.assertEqual(self.requests, [])
            queue.tick_intake()
            queue.tick()
            self.assertEqual(len(self.posts()), 1)
            self.assertEqual(fake.uploads, [])
            self.group['current_state']['status'] = 'running'
            clock.return_value = 1019
            queue.tick()
            self.assertEqual(fake.uploads, [])
            self.instance['pulling_progress'] = 8
            clock.return_value = 1022
            queue.tick()
            self.assertEqual(fake.uploads, [])
            self.instance.update(state='running', ready=True)
            clock.return_value = 1025
            queue.tick()
            self.assertEqual(len(fake.uploads), 1)
            for index in (1, 2):
                data = b'\x00\x00\x00\x18ftypisomverified-result' + str(index).encode()
                self.manager.receive(f'remote-{index}', 0, data, name=f'{index}.mp4',
                    size=len(data), sha=hashlib.sha256(data).hexdigest())
                queue.tick()
                if index == 1:
                    clock.return_value += 3
                    queue.tick()
                    self.assertEqual(len(fake.uploads), 2)
            self.assertEqual([x['state'] for x in queue.snapshot()['items']], ['Completed', 'Completed'])
            self.worker.request.return_value = {'active_renders': 0}
            with patch('app.modules.concat.engine.salad_idle.requests.get',
                       return_value=Mock(status_code=200, json=lambda: self.group)), patch(
                    'app.modules.concat.engine.salad_idle.requests.post', return_value=Mock(status_code=202)) as stop:
                clock.return_value = 1040
                queue.tick()
                clock.return_value = 1099
                queue.tick()
                stop.assert_not_called()
                clock.return_value = 1100
                queue.tick()
                stop.assert_called_once()
        self.assertEqual(len(self.posts()), 1)

    def test_manual_start_is_queued_and_uses_same_download_gate(self):
        self.manager.save({'auto_start': False})
        self.group['current_state']['status'] = 'stopped'
        self.job('Queued')
        self.manager.salad_worker.manual('start')
        self.assertEqual(self.requests, [])
        self.manager.production.tick()
        self.assertEqual(self.requests, [])
        with self.manager.connection() as con:
            con.execute("UPDATE production_jobs SET state='Cancelled'")
        with patch('app.modules.concat.engine.salad_worker.time.time', return_value=1000):
            self.manager.production.tick()
        self.assertEqual(len(self.posts()), 1)
        self.manager.production.tick()
        self.assertEqual(len(self.posts()), 1)

    def test_manual_controls_require_manual_mode(self):
        for action in ('start', 'stop'):
            with self.assertRaises(HTTPException):
                self.manager.salad_worker.manual(action)
        self.assertEqual(self.requests, [])

    def test_manual_stop_waits_for_active_work_and_is_durable(self):
        self.manager.save({'auto_start': False})
        self.job('Rendering')
        self.manager.salad_worker.manual('stop')
        with self.manager.connection() as con:
            self.manager.salad_worker.tick_manual(con)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.manager.production.snapshot()['state'], 'paused')
        with self.manager.connection() as con:
            con.execute("UPDATE production_jobs SET state='Completed'")
        self.worker.request.return_value = {'active_renders': 0}
        with patch('app.modules.concat.engine.salad_idle.requests.get', return_value=Mock(status_code=200, json=lambda: self.group)), patch(
                'app.modules.concat.engine.salad_idle.requests.post', return_value=Mock(status_code=202)) as stop:
            with self.manager.connection() as con:
                self.manager.salad_worker.tick_manual(con)
            self.manager.salad_worker.manual('stop')
            with patch('app.modules.concat.engine.salad_idle.time.time', return_value=9999999999):
                with self.manager.connection() as con:
                    self.manager.salad_worker.tick_manual(con)
            self.assertEqual(stop.call_count, 1)

    def test_stale_http_progress_is_rejected_without_mutation(self):
        response = Mock(status_code=200, headers={'Age': '60'}, json=lambda: self.group)
        with patch('app.modules.concat.engine.salad_worker.requests.request', return_value=response) as req:
            self.assertFalse(self.check(1020))
            self.assertEqual(req.call_count, 1)
        self.assertEqual(self.status(), 'api_or_worker_unavailable')

    def test_ready_during_final_confirmation_is_never_reallocated(self):
        original = self.api
        def changed(method, url, **kw):
            if method == 'GET' and url.endswith('/instances/' + ID):
                return Mock(status_code=202, json=lambda: {**self.instance, 'ready': True, 'state': 'running'})
            return original(method, url, **kw)
        with patch('app.modules.concat.engine.salad_worker.requests.request', side_effect=changed):
            self.check(1020)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.status(), 'instance_changed_rechecking')

    def test_changed_machine_on_final_confirmation_is_never_reallocated(self):
        original = self.api
        def changed(method, url, **kw):
            if method == 'GET' and url.endswith('/instances/' + ID):
                return Mock(status_code=202, json=lambda: {**self.instance, 'machine_id': ID})
            return original(method, url, **kw)
        with patch('app.modules.concat.engine.salad_worker.requests.request', side_effect=changed):
            self.check(1020)
        self.assertEqual(self.posts(), [])

    def test_three_attempt_hard_limit_for_replacement_instances(self):
        with self.assertRaises(HTTPException):
            self.manager.save({'salad_max_reallocations': 4})
        for attempt in range(1, 5):
            now = 1000 + attempt * 40
            self.instance.update(machine_id=f'{attempt}b3a4591-efc3-46c0-b06a-3d820c0ec100',
                                 update_time=self.stamp(now - 20))
            self.check(now)
        self.assertEqual(len(self.posts()), 3)
        self.assertEqual(self.status(), 'reallocation_limit')

    def test_completed_download_before_deadline_passes_without_reallocation(self):
        self.instance['state'] = 'creating'
        self.check(1005)
        self.assertEqual(self.status(), 'health_check_passed')
        self.assertEqual(self.posts(), [])
        self.instance.update(state='downloading', pulling_progress=0)
        self.check(1050)
        self.assertEqual(self.posts(), [])

    def test_new_job_cancels_shutdown_and_requires_fresh_full_idle_window(self):
        with self.manager.connection() as con:
            self.manager.put(con, 'enabled', '1')
        self.worker.request.return_value = {'active_renders': 0}
        with patch('app.modules.concat.engine.salad_idle.time.time', return_value=1000) as clock, patch(
                'app.modules.concat.engine.salad_idle.requests.get', return_value=Mock(status_code=200, json=lambda: self.group)), patch(
                'app.modules.concat.engine.salad_idle.requests.post', return_value=Mock(status_code=202)) as stop:
            self.manager.salad_idle.check()
            self.job('Queued')
            clock.return_value = 1050
            self.manager.salad_idle.check()
            with self.manager.connection() as con:
                con.execute("UPDATE production_jobs SET state='Completed'")
            clock.return_value = 1070
            self.manager.salad_idle.check()
            clock.return_value = 1129
            self.manager.salad_idle.check()
            stop.assert_not_called()
            clock.return_value = 1130
            self.manager.salad_idle.check()
            stop.assert_called_once()

    def test_service_restart_retains_timer_and_reallocation_journal(self):
        self.check(1010)
        restored = CloudRender(Path(self.tmp.name), 'a' * 64)
        self.addCleanup(restored.pool.shutdown, wait=True)
        self.manager = restored
        self.check(1020)
        self.assertEqual(len(self.posts()), 1)
        again = CloudRender(Path(self.tmp.name), 'a' * 64)
        self.addCleanup(again.pool.shutdown, wait=True)
        self.manager = again
        self.check(1100)
        self.assertEqual(len(self.posts()), 1)

    def test_ready_zip_survives_reallocation_without_drive_redownload(self):
        queue = self.manager.production
        self.job('Ready for GPU')
        archive = bundle(queue.archive(1))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.manager.connection() as con:
            con.execute("UPDATE production_jobs SET digest=?", (digest,))
        queue.drive.download = Mock(side_effect=AssertionError('verified ZIP redownloaded'))
        fake = FakeWorker()
        queue.worker_factory = lambda _: fake
        queue.control('start')
        with patch('app.modules.concat.engine.salad_worker.time.time', return_value=1020) as clock:
            queue.tick()
            self.assertEqual(len(self.posts()), 1)
            self.assertEqual(fake.uploads, [])
            self.instance.update(machine_id='fb3a4591-efc3-46c0-b06a-3d820c0ec100',
                                 update_time=self.stamp(1021), pulling_progress=8)
            clock.return_value = 1025
            queue.tick()
            self.instance.update(state='running', ready=True, update_time=self.stamp(1026))
            clock.return_value = 1030
            queue.tick()
        self.assertEqual(len(fake.uploads), 1)
        queue.drive.download.assert_not_called()
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), digest)

    def test_stale_date_and_regressing_transition_are_rejected(self):
        response = Mock(status_code=200, headers={'Date': 'Thu, 01 Jan 1970 00:00:00 GMT'}, json=lambda: self.group)
        with patch('app.modules.concat.engine.salad_worker.requests.request', return_value=response):
            self.assertFalse(self.check(1020))
        self.assertEqual(self.posts(), [])
        self.check(1030)
        self.instance['update_time'] = self.stamp(999)
        self.check(1040)
        self.assertEqual(self.status(), 'stale_instance_response')
        self.assertEqual(len(self.posts()), 1)

    def test_ambiguous_stop_is_not_repeated_and_failed_delivery_blocks_stop(self):
        with self.manager.connection() as con:
            self.manager.put(con, 'enabled', '1')
        self.worker.request.return_value = {'active_renders': 0}
        with patch('app.modules.concat.engine.salad_idle.requests.get', return_value=Mock(status_code=200, json=lambda: self.group)), patch(
                'app.modules.concat.engine.salad_idle.requests.post', side_effect=requests.Timeout('ambiguous')) as stop, patch(
                'app.modules.concat.engine.salad_idle.time.time', return_value=1000) as clock:
            self.manager.salad_idle.check()
            clock.return_value = 1060
            self.manager.salad_idle.check()
            clock.return_value = 1200
            self.manager.salad_idle.check()
            clock.return_value = 1260
            self.manager.salad_idle.check()
            self.assertEqual(stop.call_count, 1)
            self.job('Failed')
            with self.manager.connection() as con:
                con.execute("UPDATE production_jobs SET submitted_at=1")
            clock.return_value = 1500
            self.manager.salad_idle.check()
            self.assertEqual(self.manager.config()['salad_idle_status'], 'busy')
            self.assertEqual(stop.call_count, 1)

    def test_manual_stop_rejects_multiple_replicas(self):
        self.manager.save({'auto_start': False})
        self.group['replicas'] = 2
        self.worker.request.return_value = {'active_renders': 0}
        self.manager.salad_worker.manual('stop')
        with patch('app.modules.concat.engine.salad_idle.requests.get', return_value=Mock(status_code=200, json=lambda: self.group)), patch(
                'app.modules.concat.engine.salad_idle.requests.post') as stop:
            with self.manager.connection() as con:
                self.manager.salad_worker.tick_manual(con)
            stop.assert_not_called()
        self.assertEqual(self.manager.config()['salad_idle_status'], 'requires_one_replica')

    def test_download_barrier_includes_retry_and_verification(self):
        for state in ('Queued', 'Downloading', 'Verifying ZIP', 'Retrying Drive'):
            with self.subTest(state=state):
                self.job(state)
                self.assertFalse(self.check())
                self.assertEqual(self.status(), 'waiting_for_drive')
                with self.manager.connection() as con:
                    con.execute('DELETE FROM production_jobs')
        self.assertEqual(self.requests, [])

    def test_start_once_even_with_stale_get_and_restart(self):
        self.group['current_state']['status'] = 'stopped'
        self.assertFalse(self.check())
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.posts()[0].endswith('/group-one/start'))
        self.manager.salad_worker = SaladAutoWorker(self.manager)
        self.check(1005)
        self.check(1040)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.status(), 'start_pending_confirmation')

    def test_ambiguous_start_is_not_repeated_and_can_be_manually_reconciled(self):
        self.group['current_state']['status'] = 'stopped'
        self.post_error = True
        self.check()
        self.check(1005)
        self.assertEqual(len(self.posts()), 1)
        self.manager.salad_worker.retry()
        self.post_error = False
        self.check(1010)
        self.assertEqual(len(self.posts()), 2)

    def test_exact_timer_boundary_reallocates_only_once_per_transition(self):
        self.assertFalse(self.check(1000))
        self.check(1019)
        self.assertEqual(self.posts(), [])
        self.check(1022)  # poll interval is 3s
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.posts()[0].endswith('/instances/' + ID + '/reallocate'))
        self.manager.salad_worker = SaladAutoWorker(self.manager)
        self.check(1100)
        self.assertEqual(len(self.posts()), 1)

    def test_timer_uses_state_timestamp_instead_of_first_poll(self):
        self.check(1020)
        self.assertEqual(len(self.posts()), 1)

    def test_threshold_equal_eight_keeps_instance_even_if_later_sample_regresses(self):
        self.instance['pulling_progress'] = 8
        self.check(1020)
        self.instance['pulling_progress'] = 1
        self.check(1040)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.status(), 'downloading_healthy')

    def test_unknown_progress_is_not_zero(self):
        for value in (None, '0', True, float('nan'), float('inf'), -1, 101):
            self.instance['pulling_progress'] = value
            self.check(self.now + 5)
            self.assertEqual(self.status(), 'download_progress_unavailable')
        self.assertEqual(self.posts(), [])

    def test_bad_timestamp_does_not_trigger_reallocation(self):
        for stamp in (None, 'invalid', self.stamp(5000), '2026-11-22T00:00:00'):
            self.instance['update_time'] = stamp
            self.check(self.now + 5)
            self.assertEqual(self.status(), 'download_timestamp_unavailable')
        self.assertEqual(self.posts(), [])

    def test_allocating_time_is_not_download_time(self):
        self.instance['state'] = 'allocating'
        self.check(1200)
        self.assertEqual(self.posts(), [])
        self.instance.update(state='downloading', update_time=self.stamp(1200))
        self.check(1205)
        self.assertEqual(self.posts(), [])
        self.check(1220)
        self.assertEqual(len(self.posts()), 1)

    def test_new_machine_gets_own_timer_cooldown_and_budget_survive_restart(self):
        self.manager.save({'salad_max_reallocations': 1})
        self.check(1020)
        self.instance.update(machine_id='fb3a4591-efc3-46c0-b06a-3d820c0ec100', update_time=self.stamp(1021))
        self.manager.salad_worker = SaladAutoWorker(self.manager)
        self.check(1042)
        self.assertEqual(self.status(), 'reallocation_cooldown')
        self.check(1052)
        self.assertEqual(self.status(), 'reallocation_limit')
        self.assertEqual(len(self.posts()), 1)

    def test_ambiguous_reallocation_does_not_repeat(self):
        self.post_error = True
        self.check(1020)
        self.manager.salad_worker = SaladAutoWorker(self.manager)
        self.check(1100)
        self.assertEqual(len(self.posts()), 1)

    def test_api_and_worker_must_both_be_ready(self):
        self.instance.update(state='running', ready=False)
        self.assertFalse(self.check())
        self.manager.production_worker.assert_not_called()
        self.instance['ready'] = True
        from app.modules.concat.engine.cloud_render_production import WorkerWait
        self.worker.request.side_effect = WorkerWait('offline')
        self.assertFalse(self.check(1005))
        self.worker.request.side_effect = None
        self.assertTrue(self.check(1010))
        self.assertEqual(self.status(), 'ready')

    def test_real_worker_factory_sees_enabled_state_before_health_probe(self):
        from app.modules.concat.engine.cloud_render_production import Worker
        self.manager.production_worker = CloudRender.production_worker.__get__(self.manager)
        self.instance.update(state='running', ready=True)
        with patch.object(Worker, 'request', return_value={'ok': True}) as health:
            self.assertTrue(self.check())
        health.assert_called_once_with('GET', '/health/ready')
        self.assertTrue(self.manager.config()['enabled'])

    def test_active_render_never_reallocates_or_starts(self):
        self.job('Rendering')
        self.assertTrue(self.check(1200))
        self.assertEqual(self.requests, [])
        with self.assertRaises(HTTPException):
            self.manager.salad_worker.retry()

    def test_cancelled_ready_job_never_starts_gpu(self):
        self.job('Ready for GPU')
        self.manager.production.control('start')
        self.manager.production.action(1, 'cancel')
        self.manager.production.tick()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.manager.production.snapshot()['items'][0]['state'], 'Cancelled')

    def test_progress_api_failure_does_not_reallocate(self):
        with patch('app.modules.concat.engine.salad_worker.requests.request',
                   side_effect=requests.Timeout('offline')):
            self.assertFalse(self.check(1020))
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.status(), 'api_or_worker_unavailable')

    def test_configuration_and_non_dedicated_group_block_start(self):
        for field, value, expected in [('replicas', 2, 'requires_one_replica'),
                                      ('autostart_policy', True, 'disable_salad_autostart')]:
            original = self.group[field]
            self.group[field] = value
            self.check(self.now + 5)
            self.assertEqual(self.status(), expected)
            self.group[field] = original
        self.assertEqual(self.posts(), [])
        self.manager.save({'salad_container_group': ''})
        with patch.dict('os.environ', {'CONCAT_SALAD_CONTAINER_GROUP': ''}):
            self.check(1200)
        self.assertEqual(self.status(), 'not_configured')

    def test_invalid_values_are_atomic_and_secrets_never_returned(self):
        for key in ('image_pull_threshold', 'image_pull_seconds', 'salad_max_reallocations', 'salad_reallocate_cooldown'):
            for value in (-1, True, '8', 1001, 1.5):
                with self.subTest(key=key, value=value), self.assertRaises(HTTPException):
                    self.manager.save({'auto_start': False, key: value})
                self.assertTrue(self.manager.config()['auto_start'])
        with self.assertRaises(HTTPException):
            self.manager.save({'salad_project': '../other'})
        self.assertNotIn('test-management', str(self.manager.config()))
        self.assertNotIn('test-worker', str(self.manager.config()))

    def test_idle_auto_worker_stops_at_60_seconds_and_waits_for_deliveries(self):
        self.manager.save({'idle_timeout_minutes': 10, 'auto_shutdown': False})
        with self.manager.connection() as con:
            self.manager.put(con, 'enabled', '1')
        self.worker.request.return_value = {'active_renders': 0}
        group = Mock(status_code=200, json=lambda: self.group)
        with patch('app.modules.concat.engine.salad_idle.requests.get', return_value=group), patch(
                'app.modules.concat.engine.salad_idle.requests.post', return_value=Mock(status_code=202)) as post, patch(
                'app.modules.concat.engine.salad_idle.time.time', return_value=1000) as clock:
            self.manager.salad_idle.check()
            clock.return_value = 1059
            self.manager.salad_idle.check()
            post.assert_not_called()
            clock.return_value = 1060
            self.manager.salad_idle.check()
            post.assert_called_once()
            self.job('Delivering')
            clock.return_value = 1200
            self.manager.salad_idle.check()
            self.assertEqual(self.manager.config()['salad_idle_since'], 0)
            self.assertEqual(post.call_count, 1)


class EndToEndTests(unittest.TestCase):
    def test_all_zip_gate_render_delivery_and_fresh_idle_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CloudRender(Path(tmp), 'b' * 64)
            self.addCleanup(manager.pool.shutdown, wait=True)
            queue, worker = manager.production, FakeWorker()
            queue.worker_factory = lambda _: worker
            manager.save({'auto_start': False})
            # Different valid bundles: content deduplication is retained.
            source = bundle(Path(tmp) / 'source.zip')
            for index in (1, 2):
                with manager.connection() as con:
                    con.execute('''INSERT INTO production_jobs(remote_id,drive_id,name,resource_keys,
                        drive_size,drive_md5,folder_id,drive_path) VALUES(?,?,?,?,?,?,?,?)''',
                        (f'remote-{index}', f'drive-{index}', 'test.zip', '', 0, '', '', ''))
            def download(row, target):
                target.write_bytes(source.read_bytes())
                return 'test.zip'
            queue.drive.download = download
            queue.control('start')
            queue.tick_intake()
            queue.tick()
            self.assertEqual(worker.uploads, [])
            queue.update(2, state='Cancelled')
            with manager.connection() as con:
                manager.put(con, 'salad_idle_since', 500)
            queue.tick()
            self.assertEqual(len(worker.uploads), 1)
            self.assertEqual(manager.config()['salad_idle_since'], 0)
            # Existing unchanged worker push protocol delivers the MP4.
            data = b'\x00\x00\x00\x18ftypisomoffline-result'
            manager.receive('remote-1', 0, data, name='test.mp4', size=len(data), sha=hashlib.sha256(data).hexdigest())
            queue.tick()
            self.assertEqual(queue.snapshot()['items'][0]['state'], 'Completed')
            self.assertEqual(manager.local('remote-1').read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
