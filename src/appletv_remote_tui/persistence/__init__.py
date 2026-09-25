"""Local, private application data: remembered devices and file primitives.

Depends on the core models and :mod:`platformdirs` only; the pyatv credential
store in :mod:`appletv_remote_tui.apple_tv.storage` reuses the file primitives here.
"""

from appletv_remote_tui.persistence.history import JsonDeviceHistory, default_history_path
from appletv_remote_tui.persistence.private_files import (
    STORAGE_APP_NAME,
    app_data_path,
    create_private_directories,
    write_private_json,
)

__all__ = [
    "STORAGE_APP_NAME",
    "JsonDeviceHistory",
    "app_data_path",
    "create_private_directories",
    "default_history_path",
    "write_private_json",
]
