"""Offline report attribution, retention, missing-data and secret-exclusion checks."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.modules.concat.engine.cloud_render import CloudRender
from app.modules.concat.engine.telemetry import numbers, JOB_FIELDS


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = CloudRender(Path(self.tmp.name), 'a' * 64)
        self.addCleanup(self.manager.pool.shutdown, wait=True)
        self.addCleanup(self.manager.production.close)
        self.metrics = self.manager.metrics

    def job(self, index, state='Rendering'):
        with self.manager.connection() as con:
            con.execute('''INSERT INTO production_jobs(id,remote_id,drive_id,name,resource_keys,
                drive_size,drive_md5,folder_id,drive_path,state) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (index, f'remote-{index}', f'drive-{index}', 'test.zip', '', 0, '', '', '', state))

    def samples(self, kind):
        with self.manager.connection() as con:
            return [dict(r) for r in con.execute('SELECT * FROM metric_samples WHERE kind=? ORDER BY id', (kind,))]

    def test_concurrent_job_metrics_keep_separate_identities_and_ignore_secrets(self):
        self.job(1)
        self.job(2)
        self.manager.save({'concurrency': 2})
        self.metrics.observe(1, {'state': 'running', 'fps': 40, 'progress_percent': 30,
                                 'error': 'SECRET_ERROR', 'token': 'SECRET_TOKEN'})
        self.metrics.observe(2, {'state': 'delivered', 'average_fps': 60,
                               'speed_multiplier': 2, 'render_time_seconds': 12})
        rows = self.samples('job')
        one, two = [json.loads(r['payload']) for r in rows]
        self.assertEqual([r['job'] for r in rows], [1, 2])
        self.assertEqual(one['rendering_jobs'], [1, 2])
        self.assertEqual(one['progress_percent'], 30)
        self.assertEqual(two['progress_percent'], 100)
        self.assertEqual(two['average_fps'], 60)
        report = self.metrics.report(24)
        self.assertNotIn('SECRET', report)
        self.assertNotIn('"fps": 60', report)  # average summary is not live FPS
        self.assertIn('"average_fps": 60', report)

    def test_missing_and_invalid_metrics_are_not_invented(self):
        self.job(1)
        self.metrics.observe(1, {'state': 'running', 'fps': True, 'average_fps': float('nan'),
                               'progress_percent': 101, 'speed_multiplier': -1,
                               'render_time_seconds': '12'})
        payload = json.loads(self.samples('job')[0]['payload'])
        self.assertFalse(any(k in payload for k in JOB_FIELDS))
        self.assertEqual(numbers({'fps': 10**400}, JOB_FIELDS), {})
        self.assertNotIn('NaN', self.metrics.report(24))

    def test_history_survives_deletion_and_restart(self):
        self.job(1, 'Completed')
        self.metrics.observe(1, {'state': 'delivered', 'average_fps': 65})
        with self.manager.connection() as con:
            con.execute('DELETE FROM production_jobs WHERE id=1')
        restored = CloudRender(Path(self.tmp.name), 'a' * 64)
        self.addCleanup(restored.pool.shutdown, wait=True)
        self.addCleanup(restored.production.close)
        self.assertIn('"average_fps": 65', restored.metrics.report(24))
        self.assertGreaterEqual(len(self.samples('collector')), 2)

    def test_retention_and_row_cap(self):
        with patch('app.modules.concat.engine.telemetry.MAX_SAMPLES', 5):
            for value in range(10):
                self.metrics.event('queue', {'concurrency': 1})
            with self.manager.connection() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM metric_samples').fetchone()[0], 5)
        with patch('app.modules.concat.engine.telemetry.time.time', return_value=1000):
            self.metrics.event('queue', {'marker': 'old'})
        self.metrics.event('queue', {'marker': 'new'})
        self.assertNotIn('old', json.dumps(self.samples('queue')))

    def test_resource_collection_is_bounded_and_does_not_export_raw_api(self):
        self.job(1)
        self.manager.save({'salad_key': 'SECRET_API', 'worker_key': 'SECRET_WORKER',
            'endpoint': 'https://fixture.salad.cloud', 'salad_organization': 'org-one',
            'salad_project': 'project-one', 'salad_container_group': 'group-one'})
        self.manager.production_worker = Mock(return_value=Mock(timeout=60,
            request=Mock(return_value={'concurrency': 2, 'active_renders': 2, 'key': 'SECRET'})))
        response = Mock(status_code=200, headers={}, json=lambda: {'instances': [{
            'state': 'running', 'ready': True,
            'cpu_percent': 73, 'memory_usage_mb': 2048, 'token': 'SECRET_TOKEN',
            'gpu_utilization_percent': float('nan'), 'private_url': 'https://secret.test'}]})
        with patch('app.modules.concat.engine.telemetry.requests.get', return_value=response) as get:
            with patch('app.modules.concat.engine.telemetry.time.time', return_value=1000):
                self.metrics.collect()
            with patch('app.modules.concat.engine.telemetry.time.time', return_value=1005):
                self.metrics.collect()
            get.assert_called_once()
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
        payload = json.loads(self.samples('resources')[-1]['payload'])
        self.assertEqual(payload['cpu_percent'], 73)
        self.assertEqual(payload['memory_usage_mb'], 2048)
        self.assertNotIn('token', payload)
        with patch('app.modules.concat.engine.telemetry.time.time', return_value=1010):
            report = self.metrics.report(1)
        self.assertNotIn('SECRET', report)
        self.assertNotIn('secret.test', report)
        self.assertIn('"active_renders": 2', report)

    def test_stage_durations_and_parallel_wall_time(self):
        self.job(1, 'Queued')
        self.job(2, 'Queued')
        with patch('app.modules.concat.engine.telemetry.time.time') as clock:
            clock.return_value = 1000
            self.manager.production.update(1, state='Rendering')
            self.manager.production.update(2, state='Rendering')
            context = {'concurrency': 2, 'active_jobs': [1, 2], 'instance': 'fixture'}
            self.metrics.event('queue', context)
            clock.return_value = 1010
            self.metrics.event('queue', context)
            self.manager.production.update(1, state='Completed')
            self.manager.production.update(2, state='Completed')
            clock.return_value = 1011
            report = self.metrics.report(1)
        self.assertIn('Job 1: Rendering: 10.000s', report)
        self.assertIn('Job 2: Rendering: 10.000s', report)
        self.assertIn('observed active wall time=10.0s', report)
        self.assertNotIn('observed active wall time=20.0s', report)

    def test_retry_clears_previous_attempt_summary(self):
        self.job(1, 'Failed')
        self.metrics.observe(1, {'state': 'delivery_failed', 'average_fps': 70})
        self.manager.production.action(1, 'retry')
        with self.manager.connection() as con:
            saved = con.execute('SELECT * FROM metric_jobs WHERE job=1').fetchone()
        self.assertEqual(saved['state'], 'Queued')
        self.assertEqual(json.loads(saved['metrics']), {})

    def test_push_delivery_recovers_summary_without_changing_completion(self):
        self.job(1)
        queue = self.manager.production
        with self.manager.connection() as con:
            con.execute("UPDATE production_jobs SET endpoint='https://fixture.salad.cloud',digest=?,submitted_at=? WHERE id=1", ('b' * 64, int(time.time())))
            row = con.execute('SELECT * FROM production_jobs WHERE id=1').fetchone()
        worker = Mock(timeout=60, request=Mock(return_value={
            'state': 'delivered', 'sha256': 'b' * 64, 'render_mode': 'cuda-hybrid-v1',
            'average_fps': 77, 'speed_multiplier': 2.5}))
        queue.worker_factory = Mock(return_value=worker)
        with patch.object(self.manager, 'production_received', return_value=True), patch.object(queue, 'finish') as finish:
            queue.process(row)
            finish.assert_called_once_with(1)
        queue.worker_factory.assert_not_called()  # no network wait in dispatch
        queue.update(1, state='Completed')
        self.manager.production_worker = Mock(return_value=worker)
        self.metrics.worker_ready_at = time.time()
        self.metrics.collect_summaries()
        self.assertIn('"average_fps": 77', self.metrics.report(24))
        from app.modules.concat.engine.cloud_render_production import WorkerWait
        worker.request.side_effect = WorkerWait('private error')
        with self.manager.connection() as con:
            con.execute("UPDATE metric_jobs SET metrics='{}',sampled=0 WHERE job=1")
        self.metrics.collect_summaries()
        self.assertNotIn('private error', self.metrics.report(24))

    def test_restart_gap_is_not_reported_as_continuous_stage_duration(self):
        self.job(1)
        with patch('app.modules.concat.engine.telemetry.time.time') as clock:
            clock.return_value = 1000
            self.manager.production.update(1, state='Rendering')
            context = {'concurrency': 1, 'active_jobs': [1], 'instance': 'fixture'}
            self.metrics.event('queue', context)
            clock.return_value = 1100
            self.metrics.event('collector', {'event': 'collector_started'})
            clock.return_value = 1105
            self.metrics.event('queue', context)
            clock.return_value = 1200
            self.manager.production.update(1, state='Completed')
            report = self.metrics.report(1)
        self.assertNotIn('Rendering: 200.000s', report)
        self.assertNotIn('observed active wall time=', report)

    def test_finished_summaries_do_not_starve_later_jobs(self):
        for job in (1, 2, 3):
            self.job(job, 'Completed')
            self.metrics.observe(job, {'state': 'delivered'} if job < 3 else {'state': 'running'})
            with self.manager.connection() as con:
                con.execute("UPDATE production_jobs SET endpoint='https://fixture.salad.cloud',digest=?,submitted_at=? WHERE id=?", ('b' * 64, int(time.time()), job))
                con.execute('UPDATE metric_jobs SET sampled=0 WHERE job=?', (job,))
        worker = Mock(timeout=60, request=Mock(return_value={
            'state': 'delivered', 'sha256': 'b' * 64,
            'render_mode': 'cuda-hybrid-v1', 'average_fps': 81}))
        self.manager.production_worker = Mock(return_value=worker)
        self.metrics.worker_ready_at = time.time()
        self.metrics.collect_summaries()
        worker.request.assert_called_once_with('GET', '/api/jobs/remote-3', missing=True)
        self.assertIn('"average_fps": 81', self.metrics.report(24))

    def test_summary_recovery_needs_ready_worker_and_has_fixed_deadline(self):
        self.job(1, 'Completed')
        worker = Mock(timeout=60, request=Mock(return_value={
            'state': 'running', 'sha256': 'b' * 64, 'render_mode': 'cuda-hybrid-v1'}))
        self.manager.production_worker = Mock(return_value=worker)
        with patch('app.modules.concat.engine.telemetry.time.time') as clock:
            clock.return_value = 1000
            self.metrics.observe(1, {'state': 'running'})
            with self.manager.connection() as con:
                con.execute("UPDATE production_jobs SET endpoint='https://fixture.salad.cloud',digest=?,submitted_at=1000 WHERE id=1", ('b' * 64,))
            clock.return_value = 1100
            self.metrics.collect_summaries()
            worker.request.assert_not_called()
            self.metrics.worker_ready_at = 1100
            self.metrics.collect_summaries()
            self.assertEqual(worker.request.call_count, 1)
            clock.return_value = 1310
            self.metrics.worker_ready_at = 1310
            self.metrics.collect_summaries()
            self.assertEqual(worker.request.call_count, 1)

    def test_state_samples_use_committed_transition_and_ignore_missing_job(self):
        self.job(1, 'Queued')
        self.manager.production.update(1, state='Rendering')
        payload = json.loads(self.samples('state')[-1]['payload'])
        self.assertEqual(payload['rendering_jobs'], [1])
        self.manager.production.update(999, state='Rendering')
        self.assertFalse(any(row['job'] == 999 for row in self.samples('state')))
