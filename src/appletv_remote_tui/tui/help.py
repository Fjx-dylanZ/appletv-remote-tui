"""Modal key reference for REMOTE, TEXT NORMAL, and TEXT INSERT."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

HELP_TEXT = """REMOTE
  h j k l / arrows  Navigate        Enter  Select
  H J K L           Swipe           S      Hold Select
  Esc / Backspace   Back            g      Home / TV
  Space / p         Play / Pause    + / -  Volume
  i                 Edit TV text    d      Devices
  r                 Reconnect       ?      Help
  o / x             Power on / off  q      Quit
  ; then key        Hold for 1 second (one shot; ; again cancels)
    g Home, Enter Select, h/j/k/l/arrows Navigate, Backspace Back
    Esc cancels without Back. Other keys clear HOLD; unsupported
    remote holds are rejected, never sent as taps.

TEXT NORMAL
  h l w b e 0 ^ $   Move            i a I A  Insert / append
  x / X             Delete char     u / Ctrl-r  Undo / redo
  dw / cw            Delete/change   diw/ciw  Inner word
  D / C, dd / cc     Rest of line / whole line
  p / P              Put register    Enter    Sync + select

TEXT INSERT
  Type/paste normally; standard cursor and editing keys work.
  Enter syncs + selects. Esc moves to TEXT NORMAL.

Escape ladder: TEXT INSERT → TEXT NORMAL → REMOTE → Apple TV Back
"""


class HelpScreen(ModalScreen[None]):
    """Show the key map for every mode."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,question_mark,q", "dismiss_help", "Close", priority=True),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
        background: $background 65%;
    }

    #help-dialog {
        width: 76;
        max-width: 94%;
        height: 34;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }

    #help-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #help-copy {
        height: 1fr;
    }

    #help-text {
        height: auto;
    }

    #close-help {
        dock: bottom;
        width: 12;
        align-horizontal: right;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Label("Apple TV remote keys · ↑/↓ scroll", id="help-title")
            with VerticalScroll(id="help-copy"):
                yield Static(HELP_TEXT, id="help-text", markup=False)
            yield Button("Close", id="close-help", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#help-copy", VerticalScroll).focus()

    def action_dismiss_help(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#close-help")
    def close_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_dismiss_help()
