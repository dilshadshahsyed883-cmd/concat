"""Resolve the correct Auto Engine worker names without changing worker ownership.

The fresh GitHub production installer runs vps1-production-lum-* units.
Existing legacy LUM installations continue using their original unit names.
This module reports service status only; it never starts a scheduler or upload.
"""
from __future__ import annotations

import os


def auto_engine_service_units() -> dict[str, str]:
    if os.environ.get("FRESH_WEBSITE_INSTALL") == "1":
        instance = os.environ.get("LUM_ENGINE_INSTANCE", "engine1")
        prefix = ("vps1-production-lum-" + instance) if instance in {"engine2", "shorts", "song", "shortstudio"} else "vps1-production-lum"
    else:
        prefix = "lum-auto-engine-v6"
    return {
        "worker": prefix + "-worker.service",
        "scheduler": prefix + "-scheduler.service",
    }
