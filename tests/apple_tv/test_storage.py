from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from appletv_remote_tui.apple_tv.storage import SecureFileStorage, create_storage


async def test_storage_is_written_atomically_with_restrictive_permissions(tmp_path: Path) -> None:
    storage_path = tmp_path / "private" / "credentials.json"

    storage = await create_storage(storage_path)
    await storage.save()

    assert storage_path.exists()
    assert storage_path.stat().st_mode & 0o777 == 0o600
    assert storage_path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(storage_path.read_text()) == {"version": 1, "devices": []}
    assert not list(storage_path.parent.glob("*.tmp"))


def test_existing_storage_permissions_are_restricted(tmp_path: Path) -> None:
    storage_path = tmp_path / "credentials.json"
    storage_path.write_text('{"version": 1, "devices": []}\n')
    os.chmod(storage_path, 0o644)

    SecureFileStorage(storage_path, asyncio.new_event_loop())

    assert storage_path.stat().st_mode & 0o777 == 0o600


def test_preexisting_parent_permissions_are_not_changed(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o755)

    SecureFileStorage(tmp_path / "credentials.json", asyncio.new_event_loop())

    assert tmp_path.stat().st_mode & 0o777 == 0o755


def test_symbolic_link_storage_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"version": 1, "devices": []}\n')
    link = tmp_path / "credentials.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        SecureFileStorage(link, asyncio.new_event_loop())
