"""Interfaces the core depends on; adapters implement them.

Transport (``AppleTVBackend``, ``PairingSession``, ``RemoteSession``) and
persistence (``DeviceHistory``) are supplied by the composition root so the
controller can be exercised with in-memory implementations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from appletv_remote_tui.core.models import Capabilities, Device, KeyboardFocus, RemoteAction


class PairingSession(Protocol):
    """An in-progress PIN pairing attempt."""

    @property
    def device_provides_pin(self) -> bool:
        """Return whether the PIN is displayed by the Apple TV."""
        ...

    async def finish(self, pin: str) -> None:
        """Submit a PIN and persist credentials on success."""
        ...

    async def close(self) -> None:
        """Release pairing resources."""
        ...


class RemoteSession(Protocol):
    """A connected, authenticated Apple TV session."""

    @property
    def capabilities(self) -> Capabilities:
        """Return capabilities available for this session."""
        ...

    @property
    def keyboard_focus(self) -> KeyboardFocus:
        """Return the latest virtual-keyboard focus state."""
        ...

    async def send(self, action: RemoteAction, *, hold: bool = False) -> None:
        """Send one remote action, held down instead of tapped when ``hold`` is true."""
        ...

    async def get_text(self) -> str:
        """Return the active Apple TV text-field contents."""
        ...

    async def set_text(self, text: str) -> None:
        """Replace the active Apple TV text-field contents."""
        ...

    async def close(self) -> None:
        """Close the device connection."""
        ...


class AppleTVBackend(Protocol):
    """Discovery, pairing, and connection boundary used by the controller."""

    async def discover(
        self, hosts: tuple[str, ...] | None = None, scan_timeout: float = 5.0
    ) -> list[Device]:
        """Discover compatible Apple TVs."""
        ...

    async def begin_pairing(self, device: Device) -> PairingSession:
        """Begin pairing and prompt the device for a PIN."""
        ...

    async def connect(
        self,
        device: Device,
        *,
        on_disconnect: Callable[[str | None], None],
        on_keyboard_focus: Callable[[KeyboardFocus], None],
    ) -> RemoteSession:
        """Connect to a paired Apple TV."""
        ...

    async def forget(self, device: Device) -> None:
        """Remove locally stored settings and credentials for a device."""
        ...


class DeviceHistory(Protocol):
    """Persistence boundary for successfully connected device metadata."""

    def load(self) -> tuple[Device, ...]:
        """Load remembered devices in most-recently-connected order."""
        ...

    def save(self, devices: tuple[Device, ...]) -> None:
        """Persist remembered device metadata."""
        ...
