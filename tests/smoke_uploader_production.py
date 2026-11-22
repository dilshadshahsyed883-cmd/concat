"""Production-only import smoke: run with an isolated venv installed using 'pip install .'.

This script must not depend on pytest, TestClient, the checkout's local app
modules, a live VPS, external credentials or any publishing endpoint.
It imports the real reused uploader as it is imported on production startup,
but deliberately never runs the worker's ASGI startup hooks or uploads media.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

with tempfile.TemporaryDirectory(prefix="concat-uploader-runtime-") as tmp:
    root = Path(tmp)
    data = root / "uploader"
    data.mkdir(mode=0o700)
    os.environ.update({
        "CONCAT_UPLOADER_ENABLED": "1",
        "LUM_DATA_DIR": str(data),
        "LUM_SESSION_SECRET": "runtime-smoke-not-production-secret-1234567890",
        "CONCAT_CLOUD_RENDER_ROOT": str(root / "cloud"),
        "CONCAT_PUBLIC_ORIGIN": "https://concat.scenebuilder.online",
        "LUM_ENGINE_INSTANCE": "concat",
        "LUM_BASE_PATH": "/concat-uploader",
        "LUM_SUITE_AUTH": "1",
        "LUM_COOKIE_SECURE": "1",
        "CONCAT_WEB_IN_PROCESS": "1",
    })
    from app.modules.uploader.gateway import (
        _configure_from_private_environment,
        _verify_private_storage,
        _import_reused_app,
    )

    _configure_from_private_environment()
    _verify_private_storage()
    reused = _import_reused_app()
    assert reused.title == "Concat Uploader", reused.title
    assert "httpx" in __import__("sys").modules, "Missing production httpx import"
    print("PRODUCTION_ONLY_UPLOADER_IMPORT=PASS")
