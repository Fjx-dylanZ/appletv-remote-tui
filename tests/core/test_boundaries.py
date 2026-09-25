"""Import boundaries: the core and persistence layers never load frontend or transport code."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_BLOCKED_FOR_CORE = ("textual", "pyatv", "platformdirs")
_BLOCKED_FOR_PERSISTENCE = ("textual", "pyatv")


@pytest.mark.parametrize(
    ("package", "blocked"),
    [
        ("appletv_remote_tui.core", _BLOCKED_FOR_CORE),
        ("appletv_remote_tui.persistence", _BLOCKED_FOR_PERSISTENCE),
    ],
)
def test_layer_imports_without_blocked_packages(package: str, blocked: tuple[str, ...]) -> None:
    script = textwrap.dedent(
        f"""
        import importlib, sys
        for name in {blocked!r}:
            sys.modules[name] = None  # makes any import of the package raise ImportError
        importlib.import_module({package!r})
        """
    )

    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
