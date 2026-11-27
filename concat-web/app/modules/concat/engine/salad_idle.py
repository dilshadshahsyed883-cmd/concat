"""Opt-in, fail-closed SaladCloud idle stop for a dedicated Concat worker group.

Only the VPS production scheduler can request a stop. It must see no pending
queue, delivery or Drive intake, and a reachable worker with zero active renders.
External jobs submitted outside Concat Web are NOT discoverable from this worker API;
only use this with a dedicated group.
"""
from __future__ import annotations

import logging
import os
import re
import time

import requests

LOG = logging.getLogger(__name__)
SLUG = re.compile(r"[a-z][a-z0-9-]{0,61}[a-z0-9]\Z")
SALAD_API = "https://api.salad.com/api/public"
TERMINAL = ("Completed", "Failed", "Cancelled")


def group_path(cfg=None):
    cfg = cfg or {}
    names = (cfg.get("salad_organization") or os.getenv("CONCAT_SALAD_ORGANIZATION", ""),
             cfg.get("salad_project") or os.getenv("CONCAT_SALAD_PROJECT", ""),
             cfg.get("salad_container_group") or os.getenv("CONCAT_SALAD_CONTAINER_GROUP", ""))
    if not all(SLUG.fullmatch(name) for name in names):
        return None
    return "/organizations/{}/projects/{}/containers/{}".format(*names)


class SaladIdleStop:
    def __init__(self, manager):
        self.manager = manager

    def ready(self, con):
        return bool(self.manager.salad_group_path(con) and self.manager.get(con, "salad_key")
                    and self.manager.get(con, "endpoint")
                    and self.manager.get(con, "worker_key"))

    @staticmethod
    def ready_config(cfg):
        return bool(group_path(cfg) and cfg.get("salad_key")
                    and cfg.get("endpoint") and cfg.get("worker_key"))

    def _set(self, con, state, *, idle=None, attempt=None):
        self.manager.put(con, "salad_idle_status", state)
        if idle is not None:
            self.manager.put(con, "salad_idle_since", idle)
        if attempt is not None:
            self.manager.put(con, "salad_idle_attempt", attempt)

    def check(self):
        # The dispatcher has a cross-process flock; the manager lock additionally
        # serializes a final stop decision against job scans/retries/config saves.
        now = int(time.time())
        with self.manager.lock, self.manager.connection() as con:
            automatic = self.manager.get(con, "auto_start") == "1"
            enabled = automatic or self.manager.get(con, "auto_shutdown") == "1"
            if not enabled:
                self._set(con, "disabled", idle=0)
                return
            # Do NOT treat snapshot.active == 0 as idle: queued/Drive/retry
            # and waiting submissions must block shutdown too.
            busy = con.execute("""SELECT 1 FROM production_jobs
                WHERE state NOT IN ('Completed','Failed','Cancelled')
                   OR (state='Failed' AND submitted_at>0 AND rerender=0 AND output_sha='') LIMIT 1""").fetchone()
            transferring = con.execute("""SELECT 1 FROM files
                WHERE state IN ('receiving','queued','uploading') LIMIT 1""").fetchone()
            if busy or transferring:
                self._set(con, "busy", idle=0)
                return
            if not self.ready(con) or self.manager.get(con, "enabled") != "1":
                self._set(con, "not_configured", idle=0)
                return
            idle_since = int(self.manager.get(con, "salad_idle_since") or "0")
            if not idle_since:
                self._set(con, "countdown", idle=now)
                return
            minutes = 1 if automatic else int(self.manager.get(con, "idle_timeout_minutes") or "5")
            if now - idle_since < minutes * 60:
                self._set(con, "countdown")
                return
            self.request_stop(con, now)

    def request_stop(self, con, now, *, manual=False):
        """Shared manual/idle stop path; called only by the single dispatcher."""
        active = con.execute("""SELECT 1 FROM production_jobs WHERE state IN
            ('Waiting for Worker','Uploading to GPU','GPU Ready','Rendering','Delivering')
            OR (state='Failed' AND submitted_at>0 AND rerender=0 AND output_sha='') LIMIT 1""").fetchone()
        transferring = con.execute("SELECT 1 FROM files WHERE state IN ('receiving','queued','uploading') LIMIT 1").fetchone()
        if active or transferring:
            self._set(con, 'busy', idle=0)
            return
        if not self.ready(con):
            self._set(con, 'not_configured', idle=0)
            return
        if manual:
            # Already-stopped groups need no reachable worker to confirm success.
            try:
                from .salad_worker import SaladAutoWorker
                path = self.manager.salad_group_path(con)
                group = requests.get(SALAD_API + path, headers={
                    'Salad-Api-Key': self.manager.get(con, 'salad_key'),
                    'Accept': 'application/json', 'Cache-Control': 'no-cache'},
                    timeout=(3, 8), allow_redirects=False)
                SaladAutoWorker.check_freshness(group, now)
                if group.status_code == 200 and group.json().get('current_state', {}).get('status') in ('stopped', 'stopping'):
                    self._set(con, 'stopped', idle=0)
                    return
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                self._set(con, 'api_unavailable')
                return
        last_attempt = int(self.manager.get(con, "salad_idle_attempt") or "0")
        if now - last_attempt < 30:
            return
        self._set(con, "verifying", attempt=now)
        try:
            worker = self.manager.production_worker("", require_enabled=not manual)
            worker.timeout = min(worker.timeout, 7)
            data = worker.request("GET", "/api/config/concurrency")
            active_jobs = data.get("active_jobs") if data.get("pipeline_version") == 1 else data.get("active_renders")
            if (type(data.get("active_renders")) is not int or data["active_renders"] != 0
                    or type(active_jobs) is not int or active_jobs != 0):
                self._set(con, "worker_busy", idle=0)
                return
        except Exception:
            # Lost worker contact does NOT imply the worker is idle.
            self._set(con, "worker_unreachable")
            return
        path = self.manager.salad_group_path(con)
        if not path:
            self._set(con, "not_configured", idle=0)
            return
        headers = {"Salad-Api-Key": self.manager.get(con, "salad_key"),
                   "Accept": "application/json", "Cache-Control": "no-cache"}
        try:
            group = requests.get(SALAD_API + path, headers=headers,
                                 timeout=(3, 8), allow_redirects=False)
            if group.status_code != 200:
                self._set(con, "api_unavailable")
                return
            from .salad_worker import SaladAutoWorker
            SaladAutoWorker.check_freshness(group, now)
            data = group.json()
            state = data.get("current_state", {}).get("status") if isinstance(data, dict) else None
            if state in ("stopped", "stopping"):
                self._set(con, "stopped", idle=0)
                return
            if state != "running" or data.get("pending_change"):
                self._set(con, "api_state_uncertain")
                return
            if manual or self.manager.get(con, 'auto_start') == '1':
                if type(data.get('replicas')) is not int or data['replicas'] != 1:
                    self._set(con, 'requires_one_replica')
                    return
                if data.get('autostart_policy') is not False:
                    self._set(con, 'salad_autostart_enabled')
                    return
            if data.get("autostart_policy") is True:
                # An auto-start group could immediately resume on gateway traffic.
                self._set(con, "salad_autostart_enabled")
                return
            if self.manager.get(con, 'salad_stop_record') == path:
                self._set(con, 'stop_pending_confirmation', idle=0)
                return
            # Journal before POST so timeouts/restarts cannot repeat a stop.
            self.manager.put(con, 'salad_stop_record', path)
            self._set(con, 'stop_requested', idle=0)
            con.commit()
            # The manager lock remains held from final queue checks through
            # the HTTP POST: a concurrent scan/retry cannot slip inside.
            result = requests.post(SALAD_API + path + "/stop", headers=headers,
                                   timeout=(3, 8), allow_redirects=False)
            if result.status_code in (200, 202, 204):
                self._set(con, "stop_requested", idle=0)
                LOG.info("Salad idle container-group stop accepted")
            else:
                self._set(con, "api_unavailable")
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            # Never log API keys or response bodies.
            self._set(con, "api_unavailable")
