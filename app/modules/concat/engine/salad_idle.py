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


def group_path():
    names = (os.getenv("CONCAT_SALAD_ORGANIZATION", ""),
             os.getenv("CONCAT_SALAD_PROJECT", ""),
             os.getenv("CONCAT_SALAD_CONTAINER_GROUP", ""))
    if not all(SLUG.fullmatch(name) for name in names):
        return None
    return "/organizations/{}/projects/{}/containers/{}".format(*names)


class SaladIdleStop:
    def __init__(self, manager):
        self.manager = manager

    def ready(self, con):
        return bool(group_path() and self.manager.get(con, "salad_key")
                    and self.manager.get(con, "endpoint")
                    and self.manager.get(con, "worker_key"))

    @staticmethod
    def ready_config(cfg):
        return bool(group_path() and cfg.get("salad_key")
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
            enabled = self.manager.get(con, "auto_shutdown") == "1"
            if not enabled:
                self._set(con, "disabled", idle=0)
                return
            # Do NOT treat snapshot.active == 0 as idle: queued/Drive/retry
            # and waiting submissions must block shutdown too.
            busy = con.execute("""SELECT 1 FROM production_jobs
                WHERE state NOT IN ('Completed','Failed','Cancelled') LIMIT 1""").fetchone()
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
            minutes = int(self.manager.get(con, "idle_timeout_minutes") or "5")
            if now - idle_since < minutes * 60:
                self._set(con, "countdown")
                return
            last_attempt = int(self.manager.get(con, "salad_idle_attempt") or "0")
            if now - last_attempt < 30:
                return
            self._set(con, "verifying", attempt=now)
            try:
                worker = self.manager.production_worker("")
                worker.timeout = min(worker.timeout, 7)
                data = worker.request("GET", "/api/config/concurrency")
                if type(data.get("active_renders")) is not int or data["active_renders"] != 0:
                    self._set(con, "worker_busy", idle=0)
                    return
            except Exception:
                # Lost worker contact does NOT imply the worker is idle.
                self._set(con, "worker_unreachable")
                return
            path = group_path()
            if not path:
                self._set(con, "not_configured", idle=0)
                return
            headers = {"Salad-Api-Key": self.manager.get(con, "salad_key"),
                       "Accept": "application/json"}
            try:
                group = requests.get(SALAD_API + path, headers=headers,
                                     timeout=(3, 8), allow_redirects=False)
                if group.status_code != 200:
                    self._set(con, "api_unavailable")
                    return
                data = group.json()
                state = data.get("current_state", {}).get("status") if isinstance(data, dict) else None
                if state in ("stopped", "stopping"):
                    self._set(con, "stopped", idle=0)
                    return
                if state != "running" or data.get("pending_change"):
                    self._set(con, "api_state_uncertain")
                    return
                if data.get("autostart_policy") is True:
                    # An auto-start group could immediately resume on gateway traffic.
                    self._set(con, "salad_autostart_enabled")
                    return
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
