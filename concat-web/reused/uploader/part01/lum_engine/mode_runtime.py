"""Read-only app/engine service probes. Never access uploads, records, or credentials.

These probes only call fixed loopback health routes. A healthy HTTP service is
not proof that its uploader is configured, scheduled or publishing.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MODE_PORTS = {
    "engine2": ("LUM_MODE_ENGINE2_PORT", 8016, "/local-upload-manager/modes/engine2/health", "Local Upload Manager"),
    "shorts": ("LUM_MODE_SHORTS_PORT", 8017, "/local-upload-manager/modes/shorts/health", "Local Upload Manager"),
    "short_stream": ("SHORT_STREAM_PORT", 8145, "/health", "short-stream"),
}


def probe(mode: str) -> dict:
    if mode == "engine1":
        # Executed inside Engine 1's already authenticated web request.
        return {"web": "connected", "worker": "not_checked", "details": "Current Auto Uploader web session"}
    if mode not in MODE_PORTS:
        raise ValueError("Unknown app/engine mode")
    env, default_port, route, expected_app = MODE_PORTS[mode]
    try:
        port = int(os.environ.get(env, str(default_port)))
    except (TypeError, ValueError):
        return {"web": "not_configured", "worker": "not_checked", "details": "Invalid engine service port"}
    if not 1024 <= port <= 65535:
        return {"web": "not_configured", "worker": "not_checked", "details": "Invalid engine service port"}
    # Match the isolated app LUM_BASE_PATH configured in production/workers.py.
    # The primary /local-upload-manager/health responds only on Engine 1.
    url = f"http://127.0.0.1:{port}{route}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            if response.status != 200:
                raise ValueError("health route is not ready")
            payload = json.loads(response.read(2048).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ValueError("health check rejected")
            if payload.get("app") != expected_app:
                raise ValueError("unexpected app response")
            if mode in {"engine2", "shorts"} and payload.get("engine_instance") != mode:
                raise ValueError("different engine instance responded")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"web": "not_connected", "worker": "not_checked", "details": "Independent engine service is not responding"}
    return {
        "web": "connected",
        "worker": (
            "running" if payload.get("worker_alive") is True else
            "stopped" if payload.get("worker_alive") is False else "separate_stream_service"
        ),
        "details": "Health endpoint responding; publishing capability requires separate checks",
    }
