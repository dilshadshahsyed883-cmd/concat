from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, List, Optional

from .path_utils import CONFIG_DIR
from .secret_store import secure_file_for_current_user
from .token_utils import is_client_secret_file

SHARED_OAUTH_CLIENT_FILENAME = "shared_oauth_client.json"


def shared_oauth_client_path(config_dir: Path = CONFIG_DIR) -> Path:
    """Canonical OAuth client used for every new/reconnected YouTube channel."""
    return Path(config_dir) / SHARED_OAUTH_CLIENT_FILENAME


def get_shared_oauth_client(config_dir: Path = CONFIG_DIR) -> Optional[Path]:
    """Return the configured shared OAuth client only when it is structurally valid."""
    path = shared_oauth_client_path(config_dir)
    try:
        if path.is_file() and is_client_secret_file(path):
            return path
    except Exception:
        pass
    return None


def install_shared_oauth_client(source_path: str | Path, config_dir: Path = CONFIG_DIR) -> Path:
    """Install one OAuth client for future channel authorizations without touching tokens."""
    source = Path(source_path).expanduser()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"OAuth client JSON not found: {source}")
    if not is_client_secret_file(source):
        raise ValueError("Selected JSON is not a valid Google OAuth client-secret file.")
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    destination = shared_oauth_client_path(config_dir)
    try:
        same_file = source.resolve(strict=False) == destination.resolve(strict=False)
    except Exception:
        same_file = str(source) == str(destination)
    if not same_file:
        temporary = destination.with_name(destination.name + ".tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    secure_file_for_current_user(destination)
    if not is_client_secret_file(destination):
        raise RuntimeError("Shared OAuth client could not be installed correctly.")
    return destination


def list_oauth_client_files(config_dir: Path = CONFIG_DIR) -> List[Path]:
    """Compatibility helper: expose only the canonical shared OAuth client."""
    shared = get_shared_oauth_client(config_dir)
    return [shared] if shared else []


def oauth_client_label(path: str | Path) -> str:
    return Path(path).stem


def find_oauth_client_for_gmail(
    gmail: str,
    clients: Optional[Iterable[Path]] = None,
    *,
    config_dir: Path = CONFIG_DIR,
) -> Optional[Path]:
    """Compatibility helper: every Gmail/channel uses the same shared client."""
    if clients is not None:
        for path in clients:
            try:
                if Path(path).is_file() and is_client_secret_file(path):
                    return Path(path)
            except Exception:
                continue
        return None
    return get_shared_oauth_client(config_dir)
