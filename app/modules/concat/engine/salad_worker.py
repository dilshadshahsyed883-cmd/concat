"""VPS-only Salad lifecycle controller; no GPU image/worker protocol changes.

Called only under the production dispatcher's cross-process flock. Journals are
committed before mutations: ambiguous HTTP responses cannot duplicate actions.
A dedicated single-replica group is required.
"""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import math
import time
from uuid import UUID

import requests

from .salad_idle import SALAD_API
from .cloud_render_production import WorkerWait, JobError

INTAKE = ('Queued', 'Downloading', 'Verifying ZIP', 'Retrying Drive')
ACTIVE = ('Waiting for Worker', 'Uploading to GPU', 'Rendering', 'Delivering')
DEFAULTS = {'auto_start': '0', 'image_pull_threshold': '8', 'image_pull_seconds': '20',
            'salad_max_reallocations': '3', 'salad_reallocate_cooldown': '30'}
LIMITS = {'image_pull_threshold': (1, 100), 'image_pull_seconds': (5, 600),
          'salad_max_reallocations': (0, 3), 'salad_reallocate_cooldown': (5, 600)}
GROUP_KEYS = ('salad_organization', 'salad_project', 'salad_container_group')


def image_pull_percent(value):
    """Normalize fractional or percentage API progress to UI percentage units.

    Values in [0, 1] use the fractional convention; (1, 100] are percentages.
    The overlapping range is ambiguous without upstream unit metadata. Prefer
    keeping an instance over reallocating a potentially healthy download.
    """
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return round(value * 100, 10) if value <= 1 else value


class SaladAutoWorker:
    def __init__(self, manager):
        self.manager = manager
        with manager.connection() as con:
            for key, value in DEFAULTS.items():
                con.execute('INSERT OR IGNORE INTO config VALUES(?,?)', (key, value))

    def retry(self):
        from fastapi import HTTPException
        with self.manager.lock, self.manager.connection() as con:
            if con.execute("SELECT 1 FROM production_jobs WHERE state IN (?,?,?,?) LIMIT 1", ACTIVE).fetchone():
                raise HTTPException(409, "Wait for active GPU jobs to finish before retrying auto worker")
            self._save(con, {}, 'retry_requested')
            self.manager.put(con, 'salad_stop_record', '')
            self.manager.put(con, 'salad_manual_action', '')
            self.manager.put(con, 'salad_idle_attempt', 0)
        self.manager.production.wake.set()
        return {'ok': True}

    def manual(self, action):
        from fastapi import HTTPException
        if action not in ('start', 'stop'):
            raise HTTPException(404, "Unknown worker action")
        with self.manager.lock, self.manager.connection() as con:
            if self.manager.get(con, 'auto_start') == '1':
                raise HTTPException(409, "Select Manual mode before using worker controls")
            pending = self.manager.get(con, 'salad_manual_action')
            if pending and pending != action:
                raise HTTPException(409, "Wait for the pending worker action to finish")
            self.manager.put(con, 'salad_manual_action', action)
            if action == 'stop':
                self.manager.put(con, 'production_queue', 'paused')
            self.manager.put(con, 'salad_worker_status', 'manual_' + action + '_queued')
        self.manager.production.wake.set()
        return {'ok': True, 'action': action}

    def tick_manual(self, con):
        action = self.manager.get(con, 'salad_manual_action')
        if action == 'start':
            if self.ensure_ready(con, manual=True):
                self.manager.put(con, 'salad_manual_action', '')
        elif action == 'stop':
            self.manager.salad_idle.request_stop(con, int(time.time()), manual=True)
            state = self.manager.get(con, 'salad_idle_status')
            self.manager.put(con, 'salad_worker_status', state)
            if state in ('stopped', 'stop_requested'):
                self.manager.put(con, 'salad_manual_action', '')
        return bool(action)

    def snapshot(self, con):
        m = self.manager
        return {**{k: (m.get(con, k) == '1' if k == 'auto_start'
                       else int(m.get(con, k) or v)) for k, v in DEFAULTS.items()},
                **{k: m.get(con, k) for k in GROUP_KEYS},
                'salad_worker_status': m.get(con, 'salad_worker_status') or 'disabled',
                'salad_worker_progress': self._record(con).get('progress'),
                'salad_reallocations': self._record(con).get('attempts', 0),
                'salad_idle_status': m.get(con, 'salad_idle_status') or 'disabled',
                'salad_manual_action': m.get(con, 'salad_manual_action')}

    def _record(self, con):
        return json.loads(self.manager.get(con, 'salad_worker_record') or '{}')

    def _save(self, con, record, status):
        self.manager.put(con, 'salad_worker_record', json.dumps(record))
        self.manager.put(con, 'salad_worker_status', status)

    def _request(self, con, method, path):
        response = requests.request(method, SALAD_API + path,
            headers={'Salad-Api-Key': self.manager.get(con, 'salad_key'),
                     'Accept': 'application/json', 'Cache-Control': 'no-cache'}, timeout=(3, 8), allow_redirects=False)
        if response.status_code not in (200, 202, 204):
            raise ValueError('Salad API unavailable')
        if method == 'GET':
            self.check_freshness(response, time.time())
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('Invalid Salad response')
            return data
        return None

    @staticmethod
    def check_freshness(response, now):
        # update_time describes a state transition, not a progress sample.
        # Reject explicitly cached/old HTTP responses; never treat them as 0%.
        age = response.headers.get('Age')
        date = response.headers.get('Date')
        if isinstance(age, str) and (not age.isdigit() or int(age) > 3):
            raise ValueError('Cached Salad response')
        if isinstance(date, str):
            timestamp = parsedate_to_datetime(date).timestamp()
            if not -10 <= now - timestamp <= 15:
                raise ValueError('Stale Salad response')

    def ensure_ready(self, con, *, manual=False):
        """Return permission for a *new* dispatch; never touch active renders."""
        m, now = self.manager, time.time()
        if m.get(con, 'auto_start') != '1' and not manual:
            return True
        record = self._record(con)
        def status(value):
            self._save(con, record, value)
            return False
        if con.execute("SELECT 1 FROM production_jobs WHERE state IN (?,?,?,?) LIMIT 1", INTAKE).fetchone():
            return status('waiting_for_drive')
        # Recover active work through the established worker job protocol instead.
        if con.execute("SELECT 1 FROM production_jobs WHERE state IN (?,?,?,?) LIMIT 1", ACTIVE).fetchone():
            return True
        if con.execute("SELECT 1 FROM production_jobs WHERE state='Failed' AND submitted_at>0 AND rerender=0 AND output_sha='' LIMIT 1").fetchone():
            return status('unverified_output_pending')
        path = m.salad_group_path(con)
        if not path or not all(m.get(con, k) for k in ('salad_key', 'endpoint', 'worker_key')):
            return status('not_configured')
        if record.get('group') != path:
            record = {'group': path}
        # Rate-limit polling as well as requests, including fast queue wakeups.
        if now < record.get('poll_after', 0):
            return False
        record['poll_after'] = now + 3
        try:
            group = self._request(con, 'GET', path)
            state = group.get('current_state', {}).get('status')
            if type(group.get('replicas')) is not int or group['replicas'] != 1:
                return status('requires_one_replica')
            if group.get('autostart_policy') is not False:
                return status('disable_salad_autostart')
            if state == 'stopped' and not group.get('pending_change'):
                if record.get('start_requested'):
                    # An accepted/ambiguous start is reconciled by GET, never
                    # repeatedly POSTed while the same stopped state is visible.
                    return status('start_pending_confirmation')
                record = {'group': path, 'start_requested': True, 'poll_after': now + 3}
                m.put(con, 'salad_stop_record', '')
                self._save(con, record, 'start_requested')
                con.commit()
                self._request(con, 'POST', path + '/start')
                return False
            if state not in ('running', 'deploying') or group.get('pending_change'):
                return status('waiting_for_group')
            instances = self._request(con, 'GET', path + '/instances').get('instances')
            if not isinstance(instances, list) or len(instances) != 1:
                return status('waiting_for_instance')
            instance = instances[0]
            instance_id = str(UUID(instance['id']))
            machine_id = str(UUID(instance['machine_id']))
            identity = instance_id + '/' + machine_id + '/' + str(instance.get('version', ''))
            if record.get('instance') != identity:
                record.update(instance=identity, accepted=False, progress=None,
                              download_since=None, download_transition=None, health_passed=False, latest_update=None)
            stage = instance.get('state', '').lower()
            try:
                updated = datetime.fromisoformat(instance['update_time'].replace('Z', '+00:00'))
                if updated.tzinfo is not None:
                    value = updated.timestamp()
                    if value > now:
                        raise ValueError('Future instance timestamp')
                    if record.get('latest_update') is not None and value < record['latest_update']:
                        return status('stale_instance_response')
                    record['latest_update'] = value
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
            if instance.get('ready') is True or stage == 'creating':
                record.update(accepted=True, health_passed=True, progress=100)
            if stage == 'running' and instance.get('ready') is True:
                # API readiness alone is insufficient; check the unchanged worker.
                m.put(con, 'enabled', '1')
                con.commit()
                worker = m.production_worker('')
                worker.timeout = min(worker.timeout, 7)
                worker.request('GET', worker.health)
                # Reset the reallocation budget only after genuine readiness.
                record.update(attempts=0, start_requested=False)
                m.put(con, 'salad_stop_record', '')
                self._save(con, record, 'ready')
                return True
            if record.get('health_passed'):
                return status('health_check_passed')
            if stage != 'downloading':
                record.update(download_since=None, download_transition=None, accepted=False, progress=None)
                return status('health_check_passed' if stage == 'creating' else
                              'waiting_for_' + stage if stage in ('allocating', 'stopping') else 'allocation_state_uncertain')
            # update_time is the API's state-transition timestamp. Persist it,
            # so restarts cannot restart a slow instance's measurement window.
            transition = instance.get('update_time')
            if transition != record.get('download_transition') or record.get('download_since') is None:
                try:
                    stamp = datetime.fromisoformat(transition.replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError('Missing timezone')
                    since = stamp.timestamp()
                    if not math.isfinite(since) or since > now:
                        raise ValueError('Future timestamp')
                except (ValueError, TypeError, AttributeError):
                    return status('download_timestamp_unavailable')
                if record.get('download_since') is not None and since < record['download_since']:
                    return status('stale_instance_response')
                record.update(download_transition=transition, download_since=since, accepted=False)
            progress = image_pull_percent(instance.get('pulling_progress'))
            if progress is None:
                record['progress'] = None
                return status('download_progress_unavailable')
            record['progress'] = progress
            record['raw_progress'] = instance['pulling_progress']
            if progress >= int(m.get(con, 'image_pull_threshold') or '8'):
                record['accepted'] = True
            if record.get('accepted'):
                return status('downloading_healthy')
            if now - record['download_since'] < int(m.get(con, 'image_pull_seconds') or '20'):
                return status('checking_download')
            # Never repeat an instance/transition action even after a timeout or
            # restart. Wait for evidence of a new machine/download transition.
            if manual:
                return status('manual_download_slow')
            action = identity + '/' + str(transition)
            if record.get('reallocated') == action:
                return status('waiting_for_reallocation')
            if now < record.get('cooldown_until', 0):
                return status('reallocation_cooldown')
            if record.get('attempts', 0) >= min(3, int(m.get(con, 'salad_max_reallocations') or '0')):
                return status('reallocation_limit')
            # Re-fetch this exact instance immediately before mutation. A list
            # snapshot can race readiness or replacement on Salad's side.
            fresh = self._request(con, 'GET', path + '/instances/' + instance_id)
            if (fresh.get('id') != instance['id'] or fresh.get('machine_id') != instance['machine_id']
                    or fresh.get('version') != instance.get('version')
                    or fresh.get('update_time') != transition or fresh.get('state') != 'downloading'
                    or fresh.get('ready') is True):
                return status('instance_changed_rechecking')
            value = image_pull_percent(fresh.get('pulling_progress'))
            if value is None:
                return status('download_progress_unavailable')
            record['progress'] = value
            m.metrics.event('image_decision', {
                'instance': identity, 'download_since': record['download_since'],
                'list_raw_progress': instance['pulling_progress'],
                'confirmation_raw_progress': fresh['pulling_progress'],
                'confirmation_percent': value,
                'threshold_percent': int(m.get(con, 'image_pull_threshold') or '8'),
                'window_seconds': int(m.get(con, 'image_pull_seconds') or '20'),
                'decision': 'keep' if value >= int(m.get(con, 'image_pull_threshold') or '8') else 'reallocate',
            }, con=con)
            if value >= int(m.get(con, 'image_pull_threshold') or '8'):
                record.update(accepted=True, progress=value)
                return status('downloading_healthy')
            record.update(reallocated=action, attempts=record.get('attempts', 0) + 1,
                          cooldown_until=now + int(m.get(con, 'salad_reallocate_cooldown') or '30'))
            self._save(con, record, 'reallocate_requested')
            con.commit()
            self._request(con, 'POST', path + '/instances/' + instance_id + '/reallocate')
            return False
        except (requests.RequestException, ValueError, TypeError, KeyError,
                AttributeError, WorkerWait, JobError):
            # Never expose API response bodies, credentials or exception text.
            return status('api_or_worker_unavailable')
