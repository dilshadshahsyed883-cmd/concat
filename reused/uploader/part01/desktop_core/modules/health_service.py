from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .oauth_registry import list_oauth_client_files
from .path_utils import CONFIG_DIR, DATA_DIR, TOKEN_DIR, resolve_stored_path
from .token_utils import VALID_TOKEN, classify_google_json, is_verified_channel_id


class HealthService:
    """Fast local production-readiness checks for the command-center dashboard."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _check(status: str, component: str, details: str, action: str = "") -> Dict[str, str]:
        return {
            "status": status,
            "component": component,
            "details": details,
            "action": action,
        }

    @staticmethod
    def _directory_writable(path: Path) -> tuple[bool, str]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="health_", suffix=".tmp", dir=path)
            os.close(fd)
            Path(temp_name).unlink(missing_ok=True)
            return True, str(path)
        except Exception as exc:
            return False, f"{path}: {exc}"

    def run(self) -> List[Dict[str, str]]:
        checks: List[Dict[str, str]] = []

        try:
            with self.db.connect() as conn:
                result = str(conn.execute("PRAGMA quick_check").fetchone()[0])
                schema = conn.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()
            if result.lower() == "ok":
                checks.append(self._check("OK", "Database", f"SQLite quick_check passed; schema v{schema[0] if schema else '?'}"))
            else:
                checks.append(self._check("ERROR", "Database", result, "Restore database backup or run safe reset."))
        except Exception as exc:
            checks.append(self._check("ERROR", "Database", str(exc), "Close other copies and repair the database."))

        for label, folder in (("Data folder", DATA_DIR), ("Config folder", CONFIG_DIR), ("Default token folder", TOKEN_DIR)):
            ok, detail = self._directory_writable(folder)
            checks.append(self._check("OK" if ok else "ERROR", label, detail, "Fix permissions/path." if not ok else ""))

        try:
            channels = self.db.list_channels()
            valid = 0
            missing = 0
            invalid = 0
            unverified = 0
            for channel in channels:
                if not is_verified_channel_id(str(channel.get("channel_id") or "")):
                    unverified += 1
                raw = str(channel.get("token_file_path") or "").strip()
                path = resolve_stored_path(raw) if raw else None
                if not path or not path.exists():
                    missing += 1
                elif classify_google_json(path) == VALID_TOKEN:
                    valid += 1
                else:
                    invalid += 1
            status = "OK" if not missing and not invalid and not unverified else "WARN"
            checks.append(self._check(
                status,
                "Channel tokens",
                f"{valid} valid / {missing} missing / {invalid} invalid / {unverified} identity-unverified across {len(channels)} channel(s)",
                "Reconnect missing/invalid channels and verify every UC Channel ID." if status != "OK" else "",
            ))
        except Exception as exc:
            checks.append(self._check("ERROR", "Channel tokens", str(exc)))

        try:
            clients = list_oauth_client_files()
            checks.append(self._check(
                "OK" if clients else "WARN",
                "OAuth client registry",
                f"{len(clients)} valid client-secret JSON file(s) auto-detected in {CONFIG_DIR}",
                "Copy a valid Google OAuth client JSON into config/ and rename it to the Gmail address." if not clients else "",
            ))
        except Exception as exc:
            checks.append(self._check("ERROR", "OAuth client registry", str(exc)))

        try:
            sources = self.db.list_sheet_sources()
            stale = sum(1 for source in sources if str(source.get("json_folder_path") or "") != str(TOKEN_DIR))
            checks.append(self._check(
                "WARN" if stale else "OK",
                "Fixed token storage",
                f"data/tokens is active for {len(sources)} sheet source(s); {stale} legacy path record(s) will auto-repair",
                "Open or sync the affected Sheet source once to normalize its stored path." if stale else "",
            ))
        except Exception as exc:
            checks.append(self._check("ERROR", "Fixed token storage", str(exc)))

        try:
            safety = self.db.upload_safety_stats()
            unprotected = int(safety.get("unprotected_pending") or 0)
            review = int(safety.get("review_required") or 0)
            blocked = int(safety.get("duplicate_uploads_blocked") or 0)
            status = "ERROR" if unprotected else ("WARN" if review else "OK")
            checks.append(self._check(
                status,
                "Upload safety ledger",
                f"{blocked} duplicate attempt(s) blocked; {review} crash-review job(s); {unprotected} unprotected pending job(s)",
                "Open Queue crash review." if review else ("Refresh/rebuild pending fingerprints." if unprotected else ""),
            ))
        except Exception as exc:
            checks.append(self._check("ERROR", "Upload safety ledger", str(exc)))

        try:
            recovery = self.db.recovery_summary()
            pending = int(recovery.get("auto_resume_pending") or 0)
            review = int(recovery.get("review_required") or 0)
            checks.append(self._check(
                "WARN" if review else "OK",
                "Crash recovery",
                f"{pending} safe auto-resume pending / {review} review required",
                "Check YouTube Studio before authorizing fresh upload." if review else "",
            ))
        except Exception as exc:
            checks.append(self._check("ERROR", "Crash recovery", str(exc)))


        return checks
