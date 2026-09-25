"""Apple TV transport adapter built on pyatv's Companion protocol.

Implements the ``AppleTVBackend``, ``PairingSession``, and ``RemoteSession``
ports from :mod:`appletv_remote_tui.core.ports`.
"""

from appletv_remote_tui.apple_tv.backend import (
    DeviceNotDiscoveredError,
    PyatvBackend,
    PyatvPairingSession,
    PyatvRemoteSession,
)
from appletv_remote_tui.apple_tv.storage import (
    SecureFileStorage,
    create_storage,
    default_storage_path,
)

__all__ = [
    "DeviceNotDiscoveredError",
    "PyatvBackend",
    "PyatvPairingSession",
    "PyatvRemoteSession",
    "SecureFileStorage",
    "create_storage",
    "default_storage_path",
]
