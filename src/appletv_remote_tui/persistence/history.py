"""JSON-backed :class:`appletv_remote_tui.core.ports.DeviceHistory`."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from appletv_remote_tui.core.models import Device, DeviceAvailability
from appletv_remote_tui.persistence.private_files import app_data_path, write_private_json

HISTORY_FILENAME = "device-history.json"
HISTORY_VERSION = 1

_LOGGER = logging.getLogger(__name__)


def default_history_path() -> Path:
    """Return the platform-specific device-history path."""
    return app_data_path() / HISTORY_FILENAME


class _UnsupportedHistoryVersionError(Exception):
    """Signal that a newer history schema must be preserved unchanged."""


class JsonDeviceHistory:
    """Load and atomically save last-known metadata for connected devices."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_history_path()).expanduser()
        self._save_enabled = True

    def load(self) -> tuple[Device, ...]:
        """Load valid history records without letting a damaged file block startup."""
        try:
            path_stat = self.path.lstat()
        except FileNotFoundError:
            return ()
        except OSError as ex:
            _LOGGER.warning("Unable to inspect device history %s: %s", self.path, ex)
            return ()
        if stat.S_ISLNK(path_stat.st_mode):
            _LOGGER.warning("Ignoring symbolic-link device history: %s", self.path)
            return ()
        if not stat.S_ISREG(path_stat.st_mode):
            self._save_enabled = False
            _LOGGER.warning("Ignoring non-regular device history: %s", self.path)
            return ()

        file_descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            file_descriptor = os.open(self.path, flags)
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                self._save_enabled = False
                raise ValueError("device history is not a regular file")
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "r", encoding="utf-8") as history_file:
                file_descriptor = -1
                raw: object = json.load(history_file)
            if not isinstance(raw, dict):
                raise ValueError("unsupported device-history schema")
            document = cast("dict[object, object]", raw)
            if document.get("version") != HISTORY_VERSION:
                self._save_enabled = False
                raise _UnsupportedHistoryVersionError
            raw_records = document.get("devices")
            if not isinstance(raw_records, list):
                raise ValueError("unsupported device-history schema")
            records = cast("list[object]", raw_records)
        except _UnsupportedHistoryVersionError:
            _LOGGER.warning(
                "Ignoring unsupported device-history version in %s; leaving it unchanged",
                self.path,
            )
            return ()
        except (OSError, json.JSONDecodeError, ValueError) as ex:
            _LOGGER.warning("Ignoring unreadable device history %s: %s", self.path, ex)
            return ()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

        devices: dict[str, Device] = {}
        for record in records:
            try:
                device = _decode_device(record)
            except (TypeError, ValueError, KeyError) as ex:
                _LOGGER.warning("Ignoring invalid device-history record: %s", ex)
                continue
            previous = devices.get(device.identifier)
            if previous is None or _connected_timestamp(device) > _connected_timestamp(previous):
                devices[device.identifier] = device
        return tuple(sorted(devices.values(), key=_recency_key))

    def save(self, devices: tuple[Device, ...]) -> None:
        """Persist historical devices, excluding transient availability state."""
        if not self._save_enabled:
            _LOGGER.warning("Not overwriting unsupported device history: %s", self.path)
            return
        historical = sorted(
            (device for device in devices if device.last_connected_at is not None),
            key=_recency_key,
        )
        write_private_json(
            self.path,
            {
                "version": HISTORY_VERSION,
                "devices": [_encode_device(device) for device in historical],
            },
        )


def _connected_timestamp(device: Device) -> float:
    connected_at = device.last_connected_at
    return connected_at.timestamp() if connected_at is not None else 0.0


def _recency_key(device: Device) -> tuple[float, str, str]:
    return (-_connected_timestamp(device), device.name.casefold(), device.identifier)


def _required_string(record: dict[object, object], name: str) -> str:
    value = record[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(record: dict[object, object], name: str) -> str | None:
    value = record.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _decode_device(raw: object) -> Device:
    if not isinstance(raw, dict):
        raise TypeError("record must be an object")
    record = cast("dict[object, object]", raw)
    raw_timestamp = _required_string(record, "last_connected_at")
    try:
        connected_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        if connected_at.tzinfo is None:
            raise ValueError("last_connected_at must include a timezone")
        connected_at = connected_at.astimezone(UTC)
    except (OverflowError, ValueError) as ex:
        raise ValueError("last_connected_at must be a valid timezone-aware ISO-8601 value") from ex
    return Device(
        identifier=_required_string(record, "identifier"),
        name=_required_string(record, "name"),
        address=_required_string(record, "address"),
        model=_optional_string(record, "model"),
        os_version=_optional_string(record, "os_version"),
        last_connected_at=connected_at,
        availability=DeviceAvailability.REMEMBERED,
    )


def _encode_device(device: Device) -> dict[str, Any]:
    connected_at = device.last_connected_at
    if connected_at is None:
        raise ValueError("historical device is missing last_connected_at")
    if connected_at.tzinfo is None:
        raise ValueError("last_connected_at must include a timezone")
    return {
        "identifier": device.identifier,
        "name": device.name,
        "address": device.address,
        "model": device.model,
        "os_version": device.os_version,
        "last_connected_at": connected_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
