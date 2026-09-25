from __future__ import annotations

import json
from ipaddress import IPv4Address
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pyatv.conf import AppleTV as AppleTVConfig
from pyatv.conf import ManualService
from pyatv.const import DeviceModel, OperatingSystem, PairingRequirement, Protocol
from pyatv.interface import DeviceInfo

from appletv_remote_tui.apple_tv import DeviceNotDiscoveredError, PyatvBackend, SecureFileStorage
from appletv_remote_tui.apple_tv import backend as backend_module
from appletv_remote_tui.core import Device


def companion_config(
    *,
    paired: bool = False,
    address: str = "192.0.2.42",
) -> AppleTVConfig:
    config = AppleTVConfig(
        IPv4Address(address),
        "Living Room",
        deep_sleep=True,
        device_info=DeviceInfo(
            {
                DeviceInfo.MODEL: DeviceModel.AppleTV4KGen3,
                DeviceInfo.OPERATING_SYSTEM: OperatingSystem.TvOS,
                DeviceInfo.VERSION: "18.5",
            }
        ),
    )
    config.add_service(
        ManualService(
            "companion-id",
            Protocol.Companion,
            49152,
            {},
            credentials="paired-credentials" if paired else None,
            pairing_requirement=PairingRequirement.Mandatory,
        )
    )
    return config


async def test_discover_uses_companion_unicast_and_converts_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = AsyncMock(return_value=[companion_config(paired=True)])
    monkeypatch.setattr(backend_module.pyatv, "scan", scan)
    backend = PyatvBackend(tmp_path / "credentials.json")

    devices = await backend.discover(("192.0.2.42",), scan_timeout=2.1)

    assert devices == [
        Device(
            identifier="companion-id",
            name="Living Room",
            address="192.0.2.42",
            model="Apple TV 4K (gen 3)",
            os_version="18.5",
            deep_sleep=True,
            paired=True,
        )
    ]
    assert scan.await_args is not None
    kwargs = dict(scan.await_args.kwargs)
    storage = kwargs.pop("storage")
    assert isinstance(storage, SecureFileStorage)
    assert storage.path == tmp_path / "credentials.json"
    assert kwargs == {
        "timeout": 3,
        "protocol": Protocol.Companion,
        "hosts": ["192.0.2.42"],
    }


async def test_discover_rejects_invalid_timeout(tmp_path: Path) -> None:
    backend = PyatvBackend(tmp_path / "credentials.json")

    with pytest.raises(ValueError, match="positive finite"):
        await backend.discover(scan_timeout=0)


async def test_connect_uses_latest_config_after_address_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_config = companion_config(address="192.0.2.42")
    refreshed_config = companion_config(address="192.0.2.99")
    scan = AsyncMock(side_effect=[[original_config], [refreshed_config]])
    monkeypatch.setattr(backend_module.pyatv, "scan", scan)
    connected_atv = object()
    connect = AsyncMock(return_value=connected_atv)
    monkeypatch.setattr(backend_module.pyatv, "connect", connect)
    expected_session = object()
    session_factory = Mock(return_value=expected_session)
    monkeypatch.setattr(backend_module, "PyatvRemoteSession", session_factory)
    backend = PyatvBackend(tmp_path / "credentials.json")

    (original_device,) = await backend.discover()
    (refreshed_device,) = await backend.discover()
    session = await backend.connect(
        original_device,
        on_disconnect=lambda error: None,
        on_keyboard_focus=lambda focus: None,
    )

    assert original_device.address == "192.0.2.42"
    assert refreshed_device.identifier == original_device.identifier
    assert refreshed_device.address == "192.0.2.99"
    assert session is expected_session
    assert connect.await_args is not None
    assert connect.await_args.args[0] is refreshed_config
    assert session_factory.call_args is not None
    assert session_factory.call_args.args[0] is connected_atv


async def test_pairing_persists_credentials_and_forget_removes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = companion_config()
    scan = AsyncMock(return_value=[config])
    monkeypatch.setattr(backend_module.pyatv, "scan", scan)
    pairing = FakePairing(config)
    pair = AsyncMock(return_value=pairing)
    monkeypatch.setattr(backend_module.pyatv, "pair", pair)
    storage_path = tmp_path / "credentials.json"
    backend = PyatvBackend(storage_path)
    (device,) = await backend.discover()

    session = await backend.begin_pairing(device)
    assert session.device_provides_pin is True
    await session.finish(" 0123 ")
    await session.close()

    assert pairing.began is True
    assert pairing.pin_value == "0123"
    assert pairing.closed is True
    assert "new-credentials" in storage_path.read_text()
    assert storage_path.stat().st_mode & 0o777 == 0o600

    await backend.forget(device)

    assert json.loads(storage_path.read_text())["devices"] == []
    service = config.get_service(Protocol.Companion)
    assert service is not None
    assert service.credentials is None


async def test_begin_pairing_requires_a_discovered_device(tmp_path: Path) -> None:
    backend = PyatvBackend(tmp_path / "credentials.json")
    device = Device("missing", "Missing", "192.0.2.100")

    with pytest.raises(DeviceNotDiscoveredError, match="must be discovered"):
        await backend.begin_pairing(device)


class FakePairing:
    device_provides_pin = True

    def __init__(self, config: AppleTVConfig) -> None:
        self.config = config
        self.began = False
        self.closed = False
        self.has_paired = False
        self.pin_value: str | None = None

    async def begin(self) -> None:
        self.began = True

    def pin(self, pin: str) -> None:
        self.pin_value = pin

    async def finish(self) -> None:
        self.has_paired = True
        service = self.config.get_service(Protocol.Companion)
        assert service is not None
        service.credentials = "new-credentials"

    async def close(self) -> None:
        self.closed = True
