"""Tests for private remembered-device persistence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from appletv_remote_tui.core import Device, DeviceAvailability
from appletv_remote_tui.persistence import JsonDeviceHistory


def historical_device(
    identifier: str = "living",
    *,
    connected_at: datetime | None = None,
) -> Device:
    return Device(
        identifier=identifier,
        name="Living Røom 📺",
        address="192.0.2.10",
        model="Apple TV 4K",
        os_version="18.5",
        deep_sleep=True,
        paired=True,
        last_connected_at=connected_at or datetime(2026, 7, 18, 20, 30, tzinfo=UTC),
        availability=DeviceAvailability.CONNECTED,
    )


def test_missing_history_loads_empty(tmp_path: Path) -> None:
    assert JsonDeviceHistory(tmp_path / "device-history.json").load() == ()


def test_history_round_trip_is_atomic_private_and_ignores_transient_state(tmp_path: Path) -> None:
    history_path = tmp_path / "private" / "device-history.json"
    history = JsonDeviceHistory(history_path)
    device = historical_device()

    history.save((device,))
    loaded = history.load()

    assert loaded == (
        Device(
            identifier="living",
            name="Living Røom 📺",
            address="192.0.2.10",
            model="Apple TV 4K",
            os_version="18.5",
            last_connected_at=datetime(2026, 7, 18, 20, 30, tzinfo=UTC),
            availability=DeviceAvailability.REMEMBERED,
        ),
    )
    assert history_path.stat().st_mode & 0o777 == 0o600
    assert history_path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(history_path.parent.glob("*.tmp"))
    assert json.loads(history_path.read_text())["version"] == 1


def test_never_connected_devices_are_not_persisted(tmp_path: Path) -> None:
    history_path = tmp_path / "device-history.json"
    history = JsonDeviceHistory(history_path)

    history.save((Device("new", "New Apple TV", "192.0.2.50"),))

    assert JsonDeviceHistory(history_path).load() == ()
    assert json.loads(history_path.read_text())["devices"] == []


def test_invalid_records_do_not_discard_valid_history(tmp_path: Path) -> None:
    history_path = tmp_path / "device-history.json"
    history_path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": [
                    {
                        "identifier": "valid",
                        "name": "Den",
                        "address": "192.0.2.20",
                        "model": None,
                        "os_version": None,
                        "last_connected_at": "2026-07-18T18:00:00Z",
                    },
                    {"identifier": "missing-fields"},
                    {
                        "identifier": "overflow",
                        "name": "Overflow",
                        "address": "192.0.2.30",
                        "model": None,
                        "os_version": None,
                        "last_connected_at": "0001-01-01T00:00:00+23:59",
                    },
                    "not-an-object",
                ],
            }
        )
    )

    loaded = JsonDeviceHistory(history_path).load()

    assert [device.identifier for device in loaded] == ["valid"]


def test_duplicate_identifiers_keep_most_recent_record(tmp_path: Path) -> None:
    history_path = tmp_path / "device-history.json"
    history_path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": [
                    {
                        "identifier": "living",
                        "name": "Old name",
                        "address": "192.0.2.1",
                        "model": None,
                        "os_version": None,
                        "last_connected_at": "2026-07-17T18:00:00Z",
                    },
                    {
                        "identifier": "living",
                        "name": "Current name",
                        "address": "192.0.2.99",
                        "model": None,
                        "os_version": None,
                        "last_connected_at": "2026-07-18T18:00:00Z",
                    },
                ],
            }
        )
    )

    (loaded,) = JsonDeviceHistory(history_path).load()

    assert loaded.name == "Current name"
    assert loaded.address == "192.0.2.99"


@pytest.mark.parametrize(
    "contents",
    ["not-json", "[]", '{"version": 99, "devices": []}', '{"version": 1}'],
)
def test_malformed_history_does_not_block_startup(tmp_path: Path, contents: str) -> None:
    history_path = tmp_path / "device-history.json"
    history_path.write_text(contents)

    assert JsonDeviceHistory(history_path).load() == ()


def test_unsupported_history_version_is_not_overwritten(tmp_path: Path) -> None:
    history_path = tmp_path / "device-history.json"
    future_contents = '{"version": 99, "devices": [{"future": true}]}\n'
    history_path.write_text(future_contents)
    history = JsonDeviceHistory(history_path)

    assert history.load() == ()
    history.save((historical_device(),))

    assert history_path.read_text() == future_contents


def test_non_regular_history_path_is_not_modified(tmp_path: Path) -> None:
    history_path = tmp_path / "device-history.json"
    history_path.mkdir(mode=0o700)

    assert JsonDeviceHistory(history_path).load() == ()

    assert history_path.is_dir()
    assert history_path.stat().st_mode & 0o777 == 0o700


def test_symbolic_link_history_is_never_followed_or_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"version": 1, "devices": []}\n')
    link = tmp_path / "device-history.json"
    link.symlink_to(target)
    history = JsonDeviceHistory(link)

    assert history.load() == ()
    with pytest.raises(ValueError, match="symbolic link"):
        history.save((historical_device(),))
    assert os.path.samefile(link, target)
