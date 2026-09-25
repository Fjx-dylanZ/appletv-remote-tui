"""Toolkit- and transport-independent application core.

Importing this package pulls in only the standard library. Frontends render
:class:`AppState` and call :class:`RemoteController`; adapters implement the
protocols in :mod:`appletv_remote_tui.core.ports`.
"""

from appletv_remote_tui.core.controller import (
    Clock,
    ControllerError,
    RemoteController,
    StateListener,
)
from appletv_remote_tui.core.models import (
    AppState,
    Capabilities,
    ConnectionStatus,
    Device,
    DeviceAvailability,
    KeyboardFocus,
    RemoteAction,
)
from appletv_remote_tui.core.ports import (
    AppleTVBackend,
    DeviceHistory,
    PairingSession,
    RemoteSession,
)

__all__ = [
    "AppState",
    "AppleTVBackend",
    "Capabilities",
    "Clock",
    "ConnectionStatus",
    "ControllerError",
    "Device",
    "DeviceAvailability",
    "DeviceHistory",
    "KeyboardFocus",
    "PairingSession",
    "RemoteAction",
    "RemoteController",
    "RemoteSession",
    "StateListener",
]
