"""Render README screenshots by driving the real TUI against in-memory fakes.

Run from the repository root: ``uv run python -m scripts.render_screenshots``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from appletv_remote_tui.core import Device, RemoteController
from appletv_remote_tui.tui import RemoteTuiApp
from tests.fakes import FakeBackend

OUT = Path(__file__).resolve().parent.parent / "docs"
SIZE = (100, 30)
DEVICES = [
    Device("living", "Living Room", "192.168.1.42", paired=True),
    Device("bedroom", "Bedroom", "192.168.1.57", paired=True),
]


async def render() -> None:
    app = RemoteTuiApp(
        RemoteController(FakeBackend(DEVICES, text="severance")),
        scan_timeout=0.01,
        text_debounce=0.01,
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        app.save_screenshot("device-picker.svg", str(OUT))
        await pilot.press("enter")
        await pilot.pause(0.3)
        app.save_screenshot("remote.svg", str(OUT))
        await pilot.press("i")
        await pilot.pause(0.3)
        app.save_screenshot("text-input.svg", str(OUT))


if __name__ == "__main__":
    asyncio.run(render())
