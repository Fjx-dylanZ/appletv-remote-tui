"""In-memory implementations of the core ports shared by controller and TUI tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from appletv_remote_tui.core import Capabilities, Device, KeyboardFocus, RemoteAction

ALL_CAPABILITIES = Capabilities(
    navigation=True,
    menu=True,
    home=True,
    play_pause=True,
    volume=True,
    power=True,
    swipe=True,
    keyboard=True,
)


class Gate:
    """Block one operation until a test releases it."""

    def __init__(self) -> None:
        self.enabled = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def pass_through(self) -> None:
        if self.enabled:
            self.started.set()
            await self.release.wait()


class FakePairingSession:
    def __init__(
        self,
        *,
        device_provides_pin: bool = True,
        finish_error: Exception | None = None,
    ) -> None:
        self.device_provides_pin = device_provides_pin
        self.finish_error = finish_error
        self.finish_calls: list[str] = []
        self.close_calls = 0

    async def finish(self, pin: str) -> None:
        self.finish_calls.append(pin)
        if self.finish_error is not None:
            raise self.finish_error

    async def close(self) -> None:
        self.close_calls += 1


class FakeRemoteSession:
    def __init__(
        self,
        *,
        capabilities: Capabilities | None = None,
        keyboard_focus: KeyboardFocus = KeyboardFocus.FOCUSED,
        text: str = "",
    ) -> None:
        self.capabilities = capabilities or ALL_CAPABILITIES
        self.keyboard_focus = keyboard_focus
        self.text = text
        self.actions: list[RemoteAction] = []
        self.holds: list[RemoteAction] = []
        self.events: list[str] = []
        self.close_calls = 0
        self.set_text_error: Exception | None = None
        self.send_error: Exception | None = None
        self.first_send = Gate()
        self.set_text_gate = Gate()
        self.get_text_gate = Gate()
        self.close_gate = Gate()

    async def send(self, action: RemoteAction, *, hold: bool = False) -> None:
        """Record ``action`` in ``actions``; held ones are also recorded in ``holds``."""
        event = f"{action.value}:hold" if hold else action.value
        self.events.append(f"send-start:{event}")
        if not self.actions:
            await self.first_send.pass_through()
        if self.send_error is not None:
            raise self.send_error
        self.actions.append(action)
        if hold:
            self.holds.append(action)
        self.events.append(f"send-end:{event}")

    async def get_text(self) -> str:
        self.events.append("get-text")
        await self.get_text_gate.pass_through()
        return self.text

    async def set_text(self, text: str) -> None:
        self.events.append(f"set-text-start:{text}")
        await self.set_text_gate.pass_through()
        if self.set_text_error is not None:
            raise self.set_text_error
        self.text = text
        self.events.append(f"set-text-end:{text}")

    async def close(self) -> None:
        self.close_calls += 1
        await self.close_gate.pass_through()
        self.events.append("close")


class FakeBackend:
    def __init__(self, devices: list[Device] | None = None, *, text: str = "") -> None:
        self.discovered: list[Device] = list(devices or ())
        self.discover_calls: list[tuple[tuple[str, ...] | None, float]] = []
        self.discover_error: Exception | None = None
        self.discovery = Gate()
        self.pairing = FakePairingSession()
        self.pairing_calls: list[Device] = []
        self.session = FakeRemoteSession(text=text)
        self.connect_calls: list[Device] = []
        self.connect_error: Exception | None = None
        self.disconnect_callback: Callable[[str | None], None] | None = None
        self.focus_callback: Callable[[KeyboardFocus], None] | None = None

    async def discover(
        self,
        hosts: tuple[str, ...] | None = None,
        scan_timeout: float = 5.0,
    ) -> list[Device]:
        self.discover_calls.append((hosts, scan_timeout))
        await self.discovery.pass_through()
        if self.discover_error is not None:
            raise self.discover_error
        return list(self.discovered)

    async def begin_pairing(self, device: Device) -> FakePairingSession:
        self.pairing_calls.append(device)
        return self.pairing

    async def connect(
        self,
        device: Device,
        *,
        on_disconnect: Callable[[str | None], None],
        on_keyboard_focus: Callable[[KeyboardFocus], None],
    ) -> FakeRemoteSession:
        self.connect_calls.append(device)
        if self.connect_error is not None:
            raise self.connect_error
        self.disconnect_callback = on_disconnect
        self.focus_callback = on_keyboard_focus
        return self.session

    async def forget(self, device: Device) -> None:
        del device

    def disconnect(self, error: str | None) -> None:
        assert self.disconnect_callback is not None
        self.disconnect_callback(error)

    def set_keyboard_focus(self, focus: KeyboardFocus) -> None:
        assert self.focus_callback is not None
        self.focus_callback(focus)


class FakeDeviceHistory:
    def __init__(self, devices: tuple[Device, ...] = ()) -> None:
        self.devices = devices
        self.saved: list[tuple[Device, ...]] = []
        self.save_error: Exception | None = None

    def load(self) -> tuple[Device, ...]:
        return self.devices

    def save(self, devices: tuple[Device, ...]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(devices)
