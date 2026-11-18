from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from .path_utils import APP_ROOT

TASK_NAME = "LocalYouTubeUploadManager_AutoRun"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def auto_run_command(app_root: Path | str = APP_ROOT) -> str:
    root = Path(app_root)
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    python = root / ".venv" / "Scripts" / "python.exe"
    if pythonw.exists():
        executable = pythonw
    elif python.exists():
        executable = python
    else:
        current = Path(sys.executable)
        current_pythonw = current.with_name("pythonw.exe")
        executable = current_pythonw if current_pythonw.exists() else current
    return f'"{executable}" "{root / "app.py"}" --auto-run'


def sync_auto_engine_tasks(enabled: bool, app_root: Path | str = APP_ROOT) -> Dict[str, Any]:
    """Create or remove one safe five-minute launcher task for the current user.

    The app itself enforces a single-instance lock and database job leases, so the
    task cannot overlap a running desktop instance or duplicate queue work.
    """
    if os.name != "nt":
        return {"supported": False, "enabled": False, "message": "Windows Task Scheduler is available only on Windows."}
    if not enabled:
        result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        ok = result.returncode == 0 or "cannot find" in (result.stderr + result.stdout).lower()
        return {"supported": True, "enabled": False, "ok": ok, "message": (result.stdout or result.stderr).strip()}

    command = auto_run_command(app_root)
    result = _run([
        "schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
        "/SC", "MINUTE", "/MO", "5", "/F",
    ])
    return {
        "supported": True,
        "enabled": result.returncode == 0,
        "ok": result.returncode == 0,
        "message": (result.stdout or result.stderr).strip(),
        "command": command,
    }


def task_status() -> Dict[str, Any]:
    if os.name != "nt":
        return {"supported": False, "exists": False}
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    return {"supported": True, "exists": result.returncode == 0, "message": (result.stdout or result.stderr).strip()}
