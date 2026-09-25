"""Render README screenshots by driving the real TUI against in-memory fakes.

Run from the repository root: ``uv run python -m scripts.render_screenshots``.

Textual's SVG export links Fira Code from a CDN, but GitHub renders README images
in a sandbox that blocks external fonts. The fallback font draws block glyphs
(such as the ``▊`` input border) shorter than a cell, leaving visible seams. Each
SVG therefore embeds a subset of Fira Code containing only the glyphs it uses.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import re
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

from appletv_remote_tui.core import Device, RemoteController
from appletv_remote_tui.tui import RemoteTuiApp
from tests.fakes import FakeBackend

OUT = Path(__file__).resolve().parent.parent / "docs"
SIZE = (100, 30)
DEVICES = [
    Device("living", "Living Room", "192.168.1.42", paired=True),
    Device("bedroom", "Bedroom", "192.168.1.57", paired=True),
]
FONT_URL = "https://cdnjs.cloudflare.com/ajax/libs/firacode/6.2.0/woff2/FiraCode-{}.woff2"
FONT_SRC = re.compile(r'src: local\("FiraCode-(\w+)"\),.*?format\("woff"\);', re.DOTALL)
TEXT_CONTENT = re.compile(r"<text[^>]*>([^<]*)</text>")


async def render() -> list[Path]:
    app = RemoteTuiApp(
        RemoteController(FakeBackend(DEVICES, text="severance")),
        scan_timeout=0.01,
        text_debounce=0.01,
    )
    paths: list[Path] = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        paths.append(Path(app.save_screenshot("device-picker.svg", str(OUT))))
        await pilot.press("enter")
        await pilot.pause(0.3)
        paths.append(Path(app.save_screenshot("remote.svg", str(OUT))))
        await pilot.press("i")
        await pilot.pause(0.3)
        paths.append(Path(app.save_screenshot("text-input.svg", str(OUT))))
    return paths


def download_font(weight: str) -> bytes:
    with urllib.request.urlopen(FONT_URL.format(weight)) as response:
        return response.read()


def subset_font(font: bytes, text: str) -> str:
    """Return a base64 WOFF2 containing only the glyphs needed for ``text``."""
    tt = TTFont(io.BytesIO(font))
    subsetter = subset.Subsetter(subset.Options(flavor="woff2", layout_features=[]))
    subsetter.populate(text=text)
    subsetter.subset(tt)
    out = io.BytesIO()
    tt.flavor = "woff2"
    tt.save(out)
    return base64.b64encode(out.getvalue()).decode()


def embed_fonts(svg_path: Path, fonts: dict[str, bytes]) -> None:
    svg = svg_path.read_text()
    text = "".join(html.unescape(t) for t in TEXT_CONTENT.findall(svg))

    def replace(match: re.Match[str]) -> str:
        data = subset_font(fonts[match.group(1)], text)
        return f'src: url("data:font/woff2;base64,{data}") format("woff2");'

    svg_path.write_text(FONT_SRC.sub(replace, svg))


def main() -> None:
    paths = asyncio.run(render())
    fonts = {weight: download_font(weight) for weight in ("Regular", "Bold")}
    for path in paths:
        embed_fonts(path, fonts)


if __name__ == "__main__":
    main()
