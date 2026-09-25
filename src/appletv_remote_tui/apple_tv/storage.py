"""pyatv credential storage written with private file permissions."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pyatv.storage.file_storage import FileStorage

from appletv_remote_tui.persistence import (
    app_data_path,
    create_private_directories,
    write_private_json,
)

STORAGE_FILENAME = "credentials.json"


def default_storage_path() -> Path:
    """Return the platform-specific path used for paired-device credentials."""
    return app_data_path() / STORAGE_FILENAME


class SecureFileStorage(FileStorage):
    """A :class:`FileStorage` that writes credentials with restrictive permissions."""

    def __init__(self, filename: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.path = filename.expanduser()
        create_private_directories(self.path.parent)
        if self.path.is_symlink():
            raise ValueError(f"Credential storage must not be a symbolic link: {self.path}")
        if self.path.exists():
            os.chmod(self.path, 0o600)
        super().__init__(str(self.path), loop)

    def _save_file(self, dumped: dict[Any, Any]) -> None:
        """Atomically replace the credential file using mode ``0600``."""
        write_private_json(self.path, dumped)


async def create_storage(
    path: Path | None = None,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SecureFileStorage:
    """Create and load secure pyatv file storage."""
    active_loop = loop or asyncio.get_running_loop()
    storage = SecureFileStorage(path or default_storage_path(), active_loop)
    await storage.load()
    return storage
