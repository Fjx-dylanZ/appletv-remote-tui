"""Domain models shared by every frontend and transport adapter.

Nothing here depends on a UI toolkit, on pyatv, or on the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, auto
from typing import TypedDict


class ConnectionStatus(StrEnum):
    """Lifecycle state for the selected Apple TV."""

    IDLE = auto()
    DISCOVERING = auto()
    PAIRING = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECTING = auto()
    DISCONNECTED = auto()
    ERROR = auto()


class KeyboardFocus(StrEnum):
    """Whether tvOS currently exposes a writable text field."""

    UNKNOWN = auto()
    FOCUSED = auto()
    UNFOCUSED = auto()


class DeviceAvailability(StrEnum):
    """Transient verification state for a discovered or remembered device."""

    REMEMBERED = auto()
    CHECKING = auto()
    AVAILABLE = auto()
    UNAVAILABLE = auto()
    CONNECTED = auto()


class RemoteAction(StrEnum):
    """Protocol-independent actions understood by a remote session."""

    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    SELECT = auto()
    SWIPE_UP = auto()
    SWIPE_DOWN = auto()
    SWIPE_LEFT = auto()
    SWIPE_RIGHT = auto()
    MENU = auto()
    HOME = auto()
    PLAY_PAUSE = auto()
    VOLUME_UP = auto()
    VOLUME_DOWN = auto()
    POWER_ON = auto()
    POWER_OFF = auto()

    @property
    def label(self) -> str:
        """Return the human-readable name used in status messages."""
        return self.value.replace("_", " ").title()

    @property
    def holdable(self) -> bool:
        """Return whether the action is a button that can be held down instead of tapped."""
        return self in _HOLDABLE


_HOLDABLE = frozenset(
    {
        RemoteAction.UP,
        RemoteAction.DOWN,
        RemoteAction.LEFT,
        RemoteAction.RIGHT,
        RemoteAction.SELECT,
        RemoteAction.MENU,
        RemoteAction.HOME,
    }
)


@dataclass(frozen=True, slots=True)
class Device:
    """A discovered Apple TV identified independently of its current address."""

    identifier: str
    name: str
    address: str
    model: str | None = None
    os_version: str | None = None
    deep_sleep: bool = False
    paired: bool = False
    last_connected_at: datetime | None = None
    availability: DeviceAvailability = DeviceAvailability.AVAILABLE


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Features the active session reports as available."""

    navigation: bool = True
    menu: bool = True
    home: bool = False
    play_pause: bool = False
    volume: bool = False
    power: bool = False
    swipe: bool = False
    keyboard: bool = False

    def supports(self, action: RemoteAction, *, hold: bool = False) -> bool:
        """Return whether ``action`` may be sent through this session, tapped or held."""
        if hold and not action.holdable:
            return False
        match action:
            case (
                RemoteAction.UP
                | RemoteAction.DOWN
                | RemoteAction.LEFT
                | RemoteAction.RIGHT
                | RemoteAction.SELECT
            ):
                return self.navigation
            case (
                RemoteAction.SWIPE_UP
                | RemoteAction.SWIPE_DOWN
                | RemoteAction.SWIPE_LEFT
                | RemoteAction.SWIPE_RIGHT
            ):
                return self.swipe
            case RemoteAction.MENU:
                return self.menu
            case RemoteAction.HOME:
                return self.home
            case RemoteAction.PLAY_PAUSE:
                return self.play_pause
            case RemoteAction.VOLUME_UP | RemoteAction.VOLUME_DOWN:
                return self.volume
            case RemoteAction.POWER_ON | RemoteAction.POWER_OFF:
                return self.power


@dataclass(frozen=True, slots=True)
class AppState:
    """Observable application state published to frontends.

    ``text_editing`` is true while the focused tvOS text field is open for
    local editing; ``text_buffer`` mirrors that field and ``text_synced``
    records whether the Apple TV has acknowledged the latest buffer.
    """

    connection: ConnectionStatus = ConnectionStatus.IDLE
    devices: tuple[Device, ...] = ()
    current_device: Device | None = None
    keyboard_focus: KeyboardFocus = KeyboardFocus.UNKNOWN
    text_editing: bool = False
    text_buffer: str = ""
    text_synced: bool = True
    capabilities: Capabilities = field(default_factory=Capabilities)
    message: str = "Ready"
    error: str | None = None


class StateDelta(TypedDict, total=False):
    """Keyword-checked subset of :class:`AppState` fields for one state transition."""

    connection: ConnectionStatus
    devices: tuple[Device, ...]
    current_device: Device | None
    keyboard_focus: KeyboardFocus
    text_editing: bool
    text_buffer: str
    text_synced: bool
    capabilities: Capabilities
    message: str
    error: str | None
