"""Private, atomic JSON files under the platform-specific application data directory.

Both remembered-device history and pyatv credential storage build on these
primitives so every local file appletv-remote-tui creates is mode ``0600`` inside a
``0700`` directory, written atomically, and never a followed symbolic link.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

# Stable storage identity: branding changes must preserve existing pairings and history.
STORAGE_APP_NAME = "remote-tui"


def app_data_path() -> Path:
    """Return the per-user data directory using the stable storage identity."""
    return user_data_path(STORAGE_APP_NAME, appauthor=False)


def create_private_directories(directory: Path) -> None:
    """Create missing directories privately without changing existing parents."""
    try:
        directory.mkdir(mode=0o700)
    except FileNotFoundError:
        create_private_directories(directory.parent)
        create_private_directories(directory)
    except FileExistsError:
        if not directory.is_dir():
            raise NotADirectoryError(directory) from None
    else:
        # A restrictive umask can remove bits from mkdir's mode, so normalize only
        # directories this process demonstrably created. Existing parents (such as
        # /tmp or a user-supplied directory) must never have their mode changed.
        os.chmod(directory, 0o700)


def write_private_json(path: Path, payload: dict[Any, Any]) -> None:
    """Atomically write JSON with private parent and file permissions."""
    path = path.expanduser()
    create_private_directories(path.parent)
    if path.is_symlink():
        raise ValueError(f"Private storage must not be a symbolic link: {path}")

    temporary_path: Path | None = None
    file_descriptor = -1
    try:
        file_descriptor, raw_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(raw_path)
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as storage_file:
            file_descriptor = -1
            storage_file.write(json.dumps(payload) + "\n")
            storage_file.flush()
            os.fsync(storage_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
