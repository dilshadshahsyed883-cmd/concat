"""Owner-local, bounded performance history. Never controls worker lifecycle."""
from datetime import datetime, timezone
import fcntl
import json
import math
import re
import threading
import time
import uuid

import requests

from .salad_idle import SALAD_API

RETENTION = 7 * 86400
MAX_SAMPLES = 20000
STATES = {'Queued', 'Downloading', 'Verifying ZIP', 'Retrying Drive', 'Ready for GPU',
          'Waiting for Worker', 'Uploading to GPU', 'GPU Ready', 'Rendering', 'Delivering',
          'Completed', 'Failed', 'Cancelled'}
TERMINAL = {'Completed', 'Failed', 'Cancelled'}
JOB_FIELDS = {'progress_percent': 100, 'fps': 1000000, 'average_fps': 1000000,
              'speed_multiplier': 1000000, 'rendered_frames': 1e12,
              'total_frames': 1e12, 'render_time_seconds': 1e9,
              'video_duration_seconds': 1e9, 'delivery_bytes': 1e12,
              'delivery_seconds': 1e9, 'delivery_queue_seconds': 1e9,
              'delivery_bytes_per_second': 1e12}
RESOURCE_FIELDS = {'cpu_percent': 100000, 'memory_usage_mb': 1e9,
                   'memory_usage_percent': 100, 'gpu_utilization_percent': 100,
                   'vram_used_mb': 1e9, 'vram_total_mb': 1e9}
HARDWARE_FIELDS = {'gpu_utilization_percent': 100, 'vram_used_mb': 1e9,
                   'vram_total_mb': 1e9, 'gpu_temperature_c': 200,
                   'encoder_utilization_percent': 100}


def numbers(data, fields):
    if not isinstance(data, dict):
        return {}
    return {key: value for key, limit in fields.items()
            if type(value := data.get(key)) in (int, float)
            and 0 <= value <= limit and math.isfinite(value)}


def stamp(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds')


def effective_render_fps(payload):
    """Frames / worker render seconds, only for completed, measured summaries."""
    if not isinstance(payload, dict) or payload.get('summary') is not True:
        return None
    values = numbers(payload, JOB_FIELDS)
    frames = values.get('rendered_frames') or values.get('total_frames')
    seconds = values.get('render_time_seconds')
    if not frames or not seconds:
        return None
    return frames / seconds


class Telemetry:
    def __init__(self, manager):
        self.manager = manager
        self.stop = threading.Event()
        self.thread = None
        self.request_lock = threading.Lock()
        self.last_resource_poll = 0
        self.resource_failures = 0
        self.last_dispatch_reason = None
        self.last_dispatch_sample = 0
        self.worker_ready_at = 0
        with manager.connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS metric_samples(
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
                job INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL)''')
            con.execute('CREATE INDEX IF NOT EXISTS metric_samples_ts ON metric_samples(ts)')
            con.execute('''CREATE TABLE IF NOT EXISTS metric_jobs(
                job INTEGER PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL,
                metrics TEXT NOT NULL DEFAULT '{}', sampled REAL NOT NULL DEFAULT 0)''')
        # A restart is a real sampling gap, never silently continuous history.
        self.event('collector', {'event': 'collector_started', 'collector': uuid.uuid4().hex})

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self.run, daemon=True, name='concat-metrics')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        # Web workers/restarts share the database. Only one collector may poll
        # this owner's external APIs at a time, independently of dispatch.
        with (self.manager.root / '.metrics.lock').open('a') as lock:
            while not self.stop.is_set():
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop.wait(10)
            while not self.stop.is_set():
                try:
                    self.collect()
                    self.collect_summaries()
                except Exception:
                    # Collection must never break dispatch or expose API errors/secrets.
                    pass
                self.stop.wait(10)

    def prune(self, con, now):
        con.execute('DELETE FROM metric_samples WHERE ts < ?', (now - RETENTION,))
        con.execute('''DELETE FROM metric_samples WHERE id <=
            COALESCE((SELECT id FROM metric_samples ORDER BY id DESC LIMIT 1 OFFSET ?),0)''',
            (MAX_SAMPLES,))
        con.execute('DELETE FROM metric_jobs WHERE updated < ?', (now - RETENTION,))
        con.execute('''DELETE FROM metric_jobs WHERE job IN
            (SELECT job FROM metric_jobs ORDER BY updated DESC LIMIT -1 OFFSET ?)''', (MAX_SAMPLES,))

    def event(self, kind, payload, job=0, con=None):
        if con is None:
            with self.manager.connection() as connection:
                self.event(kind, payload, job, connection)
            return
        now = time.time()
        con.execute('INSERT INTO metric_samples(ts,kind,job,payload) VALUES(?,?,?,?)',
                    (now, kind, job, json.dumps(payload, allow_nan=False)))
        self.prune(con, now)

    def state(self, con, job, state):
        if state not in STATES:
            return
        old = con.execute('SELECT state FROM metric_jobs WHERE job=?', (job,)).fetchone()
        if old and old['state'] == state:
            return
        now = time.time()
        con.execute('''INSERT INTO metric_jobs(job,state,updated) VALUES(?,?,?)
            ON CONFLICT(job) DO UPDATE SET state=excluded.state,updated=excluded.updated''',
                    (job, state, now))
        if state in ('Queued', 'Ready for GPU') and old and old['state'] in TERMINAL:
            con.execute("UPDATE metric_jobs SET metrics='{}',sampled=0 WHERE job=?", (job,))
        self.event('state', {**self.context(con), 'state': state, 'previous': old['state'] if old else None,
                             'origin': 'transition' if old else 'first_observed'}, job, con)

    def dispatch_reason(self, con, reason, *, pipeline):
        now = time.time()
        key = (reason, pipeline)
        if key != self.last_dispatch_reason or now - self.last_dispatch_sample >= 20:
            self.event('dispatch', {**self.context(con), 'reason': reason,
                                   'pipeline_mode': 'pipeline' if pipeline else 'legacy_or_unknown'}, con=con)
            self.last_dispatch_reason, self.last_dispatch_sample = key, now

    def observe(self, job, status):
        if not isinstance(status, dict):
            return
        metrics = numbers(status, JOB_FIELDS)
        # Completed worker reports have summary metrics, not live FPS samples.
        if status.get('state') in ('rendered', 'delivered', 'delivering', 'delivery_failed'):
            metrics['progress_percent'] = 100
            metrics['summary'] = True
            if status.get('state') == 'delivering':
                metrics['delivery_pending'] = True
        with self.manager.connection() as con:
            row = con.execute('SELECT state FROM production_jobs WHERE id=?', (job,)).fetchone()
            if row is None:
                return
            self.state(con, job, row['state'])
            context = self.context(con)
            now = time.time()
            con.execute('UPDATE metric_jobs SET metrics=?,sampled=?,updated=? WHERE job=?',
                        (json.dumps(metrics), now, now, job))
            self.event('job', {**context, **metrics}, job, con)

    def context(self, con):
        rows = con.execute("SELECT id,state FROM production_jobs WHERE state NOT IN ('Completed','Failed','Cancelled')").fetchall()
        record = json.loads(self.manager.get(con, 'salad_worker_record') or '{}')
        # Instance identity comes from parsed UUIDs in the lifecycle controller.
        identity = record.get('instance', '')
        if not isinstance(identity, str) or len(identity) > 200:
            identity = ''
        return {'concurrency': int(self.manager.get(con, 'render_concurrency') or '1'),
                'rendering_jobs': [r['id'] for r in rows if r['state'] == 'Rendering'],
                'active_jobs': [r['id'] for r in rows if r['state'] in
                                ('Waiting for Worker', 'Uploading to GPU', 'GPU Ready', 'Rendering', 'Delivering')],
                'instance': identity}

    def collect_summaries(self):
        """Recover summaries missed by push delivery, entirely off dispatch."""
        from .cloud_render_production import JobError, MODE
        now = time.time()
        if not self.worker_ready_at or now - self.worker_ready_at > 30:
            return
        with self.manager.connection() as con:
            if not self.resource_poll_allowed(con, True):
                return
            rows = con.execute('''SELECT p.*,m.metrics,m.sampled FROM production_jobs p
                JOIN metric_jobs m ON m.job=p.id WHERE p.state='Completed'
                AND p.endpoint!='' AND p.submitted_at>?
                AND EXISTS(SELECT 1 FROM metric_samples s WHERE s.job=p.id
                    AND s.kind='state' AND s.ts>?
                    AND json_extract(s.payload,'$.state')='Completed')
                AND (json_extract(m.metrics,'$.summary') IS NOT 1
                     OR json_extract(m.metrics,'$.delivery_pending') IS 1)
                AND m.sampled<? ORDER BY m.sampled LIMIT 2''',
                (now - 3600, now - 300, now - 20)).fetchall()
        for row in rows:
            if self.stop.is_set():
                return
            saved = json.loads(row['metrics'])
            if saved.get('summary') and not saved.get('delivery_pending'):
                continue
            with self.manager.connection() as con:
                if not self.resource_poll_allowed(con, True):
                    return
                con.execute('UPDATE metric_jobs SET sampled=? WHERE job=?', (now, row['id']))
            try:
                worker = self.manager.production_worker(row['endpoint'], require_enabled=False)
                worker.timeout = min(worker.timeout, 5)
                status = worker.request('GET', '/api/jobs/' + row['remote_id'], missing=True)
                if (isinstance(status, dict) and status.get('sha256') == row['digest']
                        and status.get('render_mode') == MODE and not self.stop.is_set()):
                    self.observe(row['id'], status)
            except (JobError, AttributeError, TypeError):
                pass

    def resource_poll_allowed(self, con, active):
        """Use lifecycle knowledge before telemetry I/O; never wake a stopped GPU."""
        m = self.manager
        if m.get(con, 'salad_idle_status') in ('stopped', 'stop_requested', 'stop_pending_confirmation'):
            return False
        status = m.get(con, 'salad_worker_status') or ''
        if status in ('stopped', 'start_requested', 'start_pending_confirmation',
                      'waiting_for_group', 'waiting_for_instance', 'waiting_for_allocating',
                      'waiting_for_stopping', 'health_check_passed', 'allocation_state_uncertain',
                      'download_timestamp_unavailable', 'download_progress_unavailable',
                      'downloading_healthy', 'checking_download', 'manual_download_slow',
                      'waiting_for_reallocation', 'reallocation_cooldown', 'reallocation_limit',
                      'instance_changed_rechecking', 'reallocate_requested',
                      'manual_start_queued', 'manual_stop_queued'):
            return False
        # Active submitted jobs are evidence of a manually managed worker, even
        # when its lifecycle status is unknown. Ready is an explicit observation.
        return bool(active) or status == 'ready'

    def collect(self):
        m, now = self.manager, time.time()
        with m.connection() as con:
            rows = con.execute('''SELECT p.id,p.state FROM production_jobs p
                WHERE p.state NOT IN ('Completed','Failed','Cancelled')
                OR EXISTS(SELECT 1 FROM metric_jobs m WHERE m.job=p.id)''').fetchall()
            for row in rows:
                self.state(con, row['id'], row['state'])
            context = self.context(con)
            pending = any(r['state'] not in TERMINAL for r in rows)
            record = json.loads(m.get(con, 'salad_worker_record') or '{}')
            self.prune(con, now)
            stopped = m.get(con, 'salad_idle_status') in ('stopped', 'stop_requested', 'stop_pending_confirmation')
            if stopped:
                self.worker_ready_at = 0
            if not pending and (m.get(con, 'enabled') != '1' or stopped):
                self.worker_ready_at = 0
                return
            self.event('queue', {**context, 'image_percent': record.get('progress'),
                       'image_raw_progress': record.get('raw_progress'),
                       'download_since': record.get('download_since'),
                       'threshold_percent': int(m.get(con, 'image_pull_threshold') or '8'),
                       'window_seconds': int(m.get(con, 'image_pull_seconds') or '20')}, con=con)
            if not self.resource_poll_allowed(con, context['active_jobs']):
                self.worker_ready_at = 0
                return
            path, key = m.salad_group_path(con), m.get(con, 'salad_key')
        # Independent thread; never holds the manager lock during network I/O.
        interval = min(300, 20 * 2 ** min(self.resource_failures, 4))
        if not path or not key or now - self.last_resource_poll < interval:
            return
        if not self.request_lock.acquire(blocking=False):
            return
        response = None
        stage = 'request'
        try:
            self.last_resource_poll = now
            self.worker_ready_at = 0
            response = requests.get(SALAD_API + path + '/instances',
                headers={'Salad-Api-Key': key, 'Accept': 'application/json',
                         'Cache-Control': 'no-cache'}, timeout=(3, 5), allow_redirects=False)
            stage = 'http_status'
            if response.status_code not in (200, 202):
                raise ValueError('HTTP status unavailable')
            stage = 'freshness'
            m.salad_worker.check_freshness(response, time.time())
            stage = 'json'
            body = response.json()
            stage = 'instance_schema'
            instances = body.get('instances') if isinstance(body, dict) else None
            if not isinstance(instances, list):
                raise ValueError('Invalid instance collection')
            if not instances:
                self.resource_failures = 0
                self.event('resources', {**context, 'source': 'Salad API',
                           'collection_status': 'unavailable', 'reason': 'no_active_instance'})
                return
            if len(instances) != 1 or not isinstance(instances[0], dict):
                raise ValueError('Invalid instance collection')
            instance = instances[0]
            self.resource_failures = 0
            if instance.get('state') != 'running' or instance.get('ready') is not True:
                self.event('resources', {**context, 'source': 'Salad API',
                           'collection_status': 'unavailable', 'reason': 'instance_not_ready'})
                return
            payload = numbers(instance, RESOURCE_FIELDS)
            payload.update(source='Salad API', source_sample_time=None,
                           collection_status='available', **context)
            self.event('resources', payload)
            # Gateway requests may wake an autostart group. Only query an
            # instance positively observed as running and ready, never during
            # allocation or after our stop journal has been set.
            if instance.get('state') == 'running' and instance.get('ready') is True and not stopped:
                self.worker_ready_at = time.time()
                try:
                    from .cloud_render_production import JobError
                    with m.connection() as con:
                        if not self.resource_poll_allowed(con, True):
                            self.worker_ready_at = 0
                            return
                    worker = m.production_worker('')
                    worker.timeout = min(worker.timeout, 5)
                    activity = worker.request('GET', '/api/config/concurrency')
                    actual = numbers(activity, {'active_renders': 2, 'concurrency': 2,
                                                'pipeline_version': 1, 'queued_renders': 10000,
                                                'delivering': 10000, 'active_jobs': 10000})
                    if actual:
                        actual['pipeline_mode'] = 'pipeline' if activity.get('pipeline_version') == 1 else 'legacy'
                        sha = activity.get('source_sha')
                        if isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{7,64}', sha):
                            actual['source_sha'] = sha
                        self.event('worker_activity', {**context, **actual})
                    if activity.get('hardware_metrics_version') == 1:
                        with m.connection() as con:
                            if not self.resource_poll_allowed(con, True):
                                self.worker_ready_at = 0
                                return
                        hardware = worker.request('GET', '/api/metrics/hardware')
                        fields = numbers(hardware, HARDWARE_FIELDS)
                        name = hardware.get('gpu_model')
                        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9 ._()+/-]{1,120}', name):
                            fields['gpu_model'] = name
                        self.event('gpu_resources', {**context, **fields, 'source': 'CUDA worker',
                                   'collection_status': 'available' if fields else 'unavailable'})
                except (JobError, AttributeError, TypeError):
                    pass
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError) as exc:
            # Only bounded, allowlisted diagnostics: never store exception text,
            # URLs, headers, response bodies, API keys or worker credentials.
            if isinstance(exc, requests.Timeout):
                reason = 'timeout'
            elif isinstance(exc, requests.ConnectionError):
                reason = 'connection_error'
            elif isinstance(exc, requests.RequestException) and stage != 'json':
                reason = 'transport_error'
            else:
                reason = {'http_status': 'http_error',
                          'freshness': 'freshness_check_failed',
                          'json': 'invalid_json',
                          'instance_schema': 'invalid_instance_collection'}.get(stage, 'invalid_response')
            self.resource_failures += 1
            diagnostic = {**context, 'source': 'Salad API',
                          'collection_status': 'request_failed', 'failure_reason': reason}
            code = getattr(response, 'status_code', None)
            if type(code) is int and 100 <= code <= 599:
                diagnostic['http_status'] = code
            self.event('resources', diagnostic)
        finally:
            self.request_lock.release()

    def report(self, hours):
        """Bounded snapshot assembled off the event loop, then streamed by the route."""
        end = time.time()
        with self.manager.connection() as con:
            rows = con.execute('SELECT * FROM metric_samples WHERE ts>=? AND ts<=? ORDER BY ts,id LIMIT ?',
                               (end - hours * 3600, end, MAX_SAMPLES)).fetchall()
        lines = ['Concat performance report', 'Timezone: UTC', f'Generated: {stamp(end)}',
                 f'Requested range: {hours} hours',
                 'Retention: 7 days or latest 20,000 samples, whichever is shorter.',
                 'Resource times are receipt times; Salad supplies no metric sample timestamp.',
                 'Only validated reported metrics are included. Gaps and restarts are not interpolated.',
                 'FPS is worker-reported; average_fps is a completed-render summary.',
                 'Effective rendering FPS is calculated only when the completed worker reports frames and render seconds.',
                 'Concurrency comparisons are observational, not controlled benchmarks.', '']
        parsed = [(r, json.loads(r['payload'])) for r in rows]
        if rows:
            lines += [f'Available range: {stamp(rows[0]["ts"])} to {stamp(rows[-1]["ts"])}']
        else:
            lines += ['No samples in requested range.']
        resource_samples = [p for r, p in parsed if r['kind'] == 'resources']
        successes = [p for p in resource_samples if p.get('collection_status') == 'available']
        failures = [p for p in resource_samples if p.get('collection_status') == 'request_failed']
        lines += ['', 'Salad API collection health:']
        unavailable = [p for p in resource_samples if p.get('collection_status') == 'unavailable']
        lines.append(f'Available samples={len(successes)}; failed attempts={len(failures)}; unavailable samples={len(unavailable)}')
        for reason in ('no_active_instance', 'instance_not_ready'):
            count = sum(p.get('reason') == reason for p in unavailable)
            if count:
                lines.append(f'{reason}: {count}')
        reasons = {}
        for payload in failures:
            key = payload.get('failure_reason', 'unknown')
            code = payload.get('http_status')
            if type(code) is int:
                key += f' (HTTP {code})'
            reasons[key] = reasons.get(key, 0) + 1
        for reason, count in sorted(reasons.items()):
            lines.append(f'{reason}: {count}')
        if not successes:
            lines.append('No validated Salad resource readings in this range; this does not mean zero utilization.')
        for key in RESOURCE_FIELDS:
            values = [p[key] for p in successes if key in p]
            if values:
                lines.append(f'{key}: sample mean={sum(values)/len(values):.2f}; peak={max(values):.2f}; n={len(values)}')
        hardware = [p for r, p in parsed if r['kind'] == 'gpu_resources' and p.get('collection_status') == 'available']
        lines += ['', 'CUDA worker hardware readings:']
        if not hardware:
            lines.append('No validated worker hardware readings in this range.')
        for name in sorted({p['gpu_model'] for p in hardware if 'gpu_model' in p}):
            lines.append(f'GPU model: {name}')
        for key in HARDWARE_FIELDS:
            values = [p[key] for p in hardware if key in p]
            if values:
                lines.append(f'{key}: sample mean={sum(values)/len(values):.2f}; peak={max(values):.2f}; n={len(values)}')
        activity = [p for r, p in parsed if r['kind'] == 'worker_activity']
        if activity:
            last = activity[-1]
            lines += ['', 'Latest worker capability: ' + str(last.get('pipeline_mode', 'unknown')) +
                      '; source SHA=' + str(last.get('source_sha', 'unknown'))]
        dispatch = [p for r, p in parsed if r['kind'] == 'dispatch']
        if dispatch:
            lines += ['', 'Latest dispatch reason: ' + str(dispatch[-1].get('reason', 'unknown'))]
        for row, payload in parsed:
            if row['kind'] == 'delivery':
                lines.append(f"Job {row['job']}: VPS pull bytes={payload.get('delivery_bytes')}; seconds={payload.get('delivery_seconds')}; bytes/sec={payload.get('delivery_bytes_per_second')}")
        summaries = {}
        for row, payload in parsed:
            if row['kind'] == 'job':
                summaries[row['job']] = payload
        lines += ['', 'Latest per-job worker metrics (survive media deletion):']
        for job, payload in summaries.items():
            lines.append(f'Job {job}: ' + json.dumps(payload, ensure_ascii=False))
            calculated = effective_render_fps(payload)
            if calculated is not None:
                lines.append(f'Job {job}: effective rendering FPS={calculated:.2f} '
                             '(completed worker frames / worker render seconds; calculated)')
        lines += ['', 'Observed concurrency comparison (configured slots, unequal workloads):']
        coverage = {1: 0.0, 2: 0.0}
        completions = {1: set(), 2: set()}
        previous_queue = None
        gaps = []
        for row, payload in parsed:
            if row['kind'] == 'collector':
                previous_queue = None
                continue
            if row['kind'] == 'queue':
                if previous_queue:
                    ts, prev = previous_queue
                    delta = row['ts'] - ts
                    slots = prev.get('concurrency')
                    if delta > 35:
                        gaps.append((ts, row['ts']))
                    elif (0 < delta <= 35 and slots in coverage
                          and payload.get('concurrency') == slots
                          and payload.get('instance') == prev.get('instance')
                          and prev.get('active_jobs')):
                        coverage[slots] += delta
                previous_queue = row['ts'], payload
            elif (row['kind'] == 'state' and payload['state'] == 'Completed'
                  and payload.get('previous') and previous_queue):
                ts, prev = previous_queue
                slots = payload.get('concurrency')
                if (slots in completions and prev.get('concurrency') == slots
                        and 0 <= row['ts'] - ts <= 35):
                    completions[slots].add(row['job'])
        for slots in (1, 2):
            if coverage[slots] > 0:
                count = len(completions[slots])
                lines.append(f'{slots} slots: observed active wall time={coverage[slots]:.1f}s; '
                             f'completed jobs={count}; observed jobs/hour={count*3600/coverage[slots]:.2f}')
                measured = [summaries[j] for j in completions[slots]
                            if j in summaries and effective_render_fps(summaries[j]) is not None]
                if measured:
                    total_frames = sum((p.get('rendered_frames') or p.get('total_frames')) for p in measured)
                    total_render_seconds = sum(p['render_time_seconds'] for p in measured)
                    lines.append(f'{slots} slots: completed worker summaries with measured frames/time='
                                 f'{len(measured)}; frame-weighted effective rendering FPS='
                                 f'{total_frames/total_render_seconds:.2f} '
                                 '(sum of per-job frames / sum of per-job render seconds; '
                                 'not combined GPU throughput)')
        if not all(coverage.values()):
            lines.append('Both concurrency settings have not yet accumulated measured coverage.')
        for start, stop in gaps:
            lines.append(f'Sampling gap: {stamp(start)} to {stamp(stop)} ({stop-start:.1f}s)')
        lines += ['', 'State intervals (observed wall seconds; parallel durations are not added):']
        previous = {}
        for row, payload in parsed:
            if row['kind'] == 'collector':
                previous.clear()
                continue
            if row['kind'] != 'state':
                continue
            job = row['job']
            if job in previous:
                ts, state = previous[job]
                lines.append(f'Job {job}: {state}: {row["ts"]-ts:.3f}s')
            previous[job] = row['ts'], payload['state']
        lines += ['', 'Chronological samples:']
        for row, payload in parsed:
            lines.append(f'{stamp(row["ts"])} | {row["kind"]} | job={row["job"]} | ' +
                         json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return '\n'.join(lines) + '\n'
