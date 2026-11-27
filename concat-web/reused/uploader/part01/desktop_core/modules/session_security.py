from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

# These fields can contain reusable proxy credentials. They must never be
# persisted in data/ui_session.json. Database-backed channel credentials remain
# protected by Windows DPAPI through modules.secret_store.
SENSITIVE_SESSION_KEYS = frozenset({"proxy_password", "full_proxy"})


def sanitize_ui_session(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deep-copied session payload with sensitive fields removed."""

    source: Any = deepcopy(dict(data or {}))

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key).strip().lower() not in SENSITIVE_SESSION_KEYS
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, tuple):
            return [clean(item) for item in value]
        return value

    cleaned = clean(source)
    return cleaned if isinstance(cleaned, dict) else {}


def write_ui_session_securely(path: Path | str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically persist a sanitized UI session and return what was written."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_ui_session(data)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)
    return sanitized
