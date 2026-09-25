"""Command-line entry point and composition root.

This is the only module that wires concrete adapters (pyatv transport, JSON
device history) to the core controller and the Textual frontend.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from appletv_remote_tui import __version__
from appletv_remote_tui.apple_tv import PyatvBackend
from appletv_remote_tui.core import RemoteController
from appletv_remote_tui.persistence import JsonDeviceHistory
from appletv_remote_tui.tui import RemoteTuiApp


def positive_float(value: str) -> float:
    """Parse a strictly positive command-line floating-point value."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the ``atv-remote`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="atv-remote",
        description="A Vim-friendly terminal remote for Apple TV",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--host",
        action="append",
        metavar="IP",
        help="discover a specific Apple TV IP (repeatable)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=positive_float,
        default=5.0,
        metavar="SECONDS",
        help="Bonjour discovery timeout (default: 5)",
    )
    return parser


def build_app(args: argparse.Namespace) -> RemoteTuiApp:
    """Compose the production backend, history, controller, and Textual app."""
    hosts: list[str] | None = args.host
    scan_timeout: float = args.scan_timeout
    controller = RemoteController(PyatvBackend(), history=JsonDeviceHistory())
    return RemoteTuiApp(
        controller,
        hosts=tuple(hosts) if hosts else None,
        scan_timeout=scan_timeout,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Parse options and run the terminal remote."""
    build_app(build_parser().parse_args(argv)).run()
