"""Modal prompt for the PIN displayed during Companion pairing."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from appletv_remote_tui.core import Device


class PinPairingScreen(ModalScreen[str | None]):
    """Prompt for the pairing PIN; dismisses with the PIN or ``None`` on cancel."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = """
    PinPairingScreen {
        align: center middle;
        background: $background 65%;
    }

    #pin-dialog {
        width: 58;
        max-width: 90%;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }

    #pin-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #pin-input {
        margin: 1 0;
    }

    #pin-error {
        color: $error;
        height: 1;
    }

    #pin-actions {
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }

    #pin-actions Button {
        margin-left: 1;
    }
    """

    def __init__(self, device: Device, *, device_provides_pin: bool = True) -> None:
        super().__init__()
        self.device = device
        self.device_provides_pin = device_provides_pin

    def compose(self) -> ComposeResult:
        with Vertical(id="pin-dialog"):
            yield Label(f"Pair with {self.device.name}", id="pin-title", markup=False)
            prompt = (
                "Enter the PIN shown on your Apple TV."
                if self.device_provides_pin
                else "Enter the PIN requested during pairing."
            )
            yield Label(prompt, id="pin-prompt")
            yield Input(
                placeholder="PIN",
                password=True,
                restrict=r"[0-9]*",
                max_length=8,
                id="pin-input",
            )
            yield Static("", id="pin-error", markup=False)
            with Horizontal(id="pin-actions"):
                yield Button("Cancel", id="cancel-pin")
                yield Button("Pair", id="submit-pin", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#pin-input", Input).focus()

    def _submit(self) -> None:
        pin = self.query_one("#pin-input", Input).value.strip()
        if not pin:
            self.query_one("#pin-error", Static).update("Enter the PIN to continue")
            return
        self.dismiss(pin)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#pin-input")
    def pin_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    @on(Button.Pressed, "#submit-pin")
    def submit_pin_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._submit()

    @on(Button.Pressed, "#cancel-pin")
    def cancel_pin_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()
