"""Discovery screen: list, verify, and select Apple TVs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

from appletv_remote_tui.core import AppState, Device, DeviceAvailability, RemoteController
from appletv_remote_tui.tui.screen import ControllerScreen

_SELECTABLE = frozenset({DeviceAvailability.AVAILABLE, DeviceAvailability.CONNECTED})


class DeviceListItem(ListItem):
    """A device row that retains the corresponding application model."""

    def __init__(self, device: Device) -> None:
        self.device = device
        status = {
            DeviceAvailability.REMEMBERED: "remembered · not checked",
            DeviceAvailability.CHECKING: "remembered · checking…",
            DeviceAvailability.AVAILABLE: (
                "available · paired" if device.paired else "available · pairing required"
            ),
            DeviceAvailability.UNAVAILABLE: "remembered · not found",
            DeviceAvailability.CONNECTED: "connected",
        }[device.availability]
        details = " · ".join(value for value in (device.model, device.os_version) if value)
        suffix = f" · {details}" if details else ""
        if device.last_connected_at is not None:
            connected = device.last_connected_at.astimezone().strftime("%Y-%m-%d %H:%M")
            suffix += f" · last connected {connected}"
        super().__init__(
            Static(
                f"{device.name}\n{device.address} · {status}{suffix}",
                classes="device-description",
                markup=False,
            ),
            classes=f"device-{device.availability.value}",
            disabled=device.availability not in _SELECTABLE,
        )


class DevicePickerScreen(ControllerScreen):
    """Discover Apple TVs and let the user select one."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Next", show=False),
        Binding("k", "cursor_up", "Previous", show=False),
        Binding("r", "discover", "Discover", show=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    CSS = """
    DevicePickerScreen {
        align: center middle;
    }

    #device-picker-shell {
        width: 86;
        max-width: 94%;
        height: 38;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }

    #picker-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #device-list {
        height: 1fr;
        border: solid $panel;
        margin: 1 0;
    }

    DeviceListItem {
        height: 3;
        padding: 0 1;
    }

    DeviceListItem.--highlight {
        background: $accent 28%;
    }

    .device-description {
        height: 2;
    }

    #manual-row {
        height: auto;
        layout: horizontal;
    }

    #manual-host {
        width: 1fr;
        margin-right: 1;
    }

    #picker-status {
        height: auto;
        min-height: 1;
        color: $text-muted;
    }

    #picker-error {
        height: auto;
        min-height: 1;
        color: $error;
    }
    """

    def __init__(
        self,
        controller: RemoteController,
        *,
        hosts: tuple[str, ...] | None = None,
        scan_timeout: float = 5.0,
        auto_discover: bool = True,
    ) -> None:
        super().__init__(controller)
        self.hosts = hosts
        self.scan_timeout = scan_timeout
        self.auto_discover = auto_discover
        self._device_snapshot: tuple[Device, ...] = ()
        self._selection_busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="device-picker-shell"):
            yield Label("Apple TV devices", id="picker-title")
            yield Label(
                "Select a paired device, or pair a newly discovered one.",
                id="picker-instructions",
            )
            yield ListView(id="device-list")
            with Horizontal(id="manual-row"):
                yield Input(placeholder="Optional IP address", id="manual-host")
                yield Button("Discover", id="discover", variant="primary")
            yield Static("Ready", id="picker-status", markup=False)
            yield Static("", id="picker-error", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.call_later(lambda: self.update_state(self.controller.state))
        if self.auto_discover:
            self.run_worker(self._discover(), group="discovery", exclusive=True)

    def update_state(self, state: AppState) -> None:
        if not self.is_mounted:
            return
        self.query_one("#picker-status", Static).update(state.message)
        self.query_one("#picker-error", Static).update(state.error or "")
        if state.devices != self._device_snapshot:
            self._device_snapshot = state.devices
            self.run_worker(
                self._replace_devices(state.devices),
                group="device-list",
                exclusive=True,
            )

    async def _replace_devices(self, devices: Iterable[Device]) -> None:
        device_list = self.query_one("#device-list", ListView)
        highlighted = device_list.highlighted_child
        highlighted_identifier = (
            highlighted.device.identifier if isinstance(highlighted, DeviceListItem) else None
        )
        await device_list.clear()
        items = [DeviceListItem(device) for device in devices]
        if items:
            await device_list.extend(items)
            device_list.index = next(
                (
                    index
                    for index, item in enumerate(items)
                    if item.device.identifier == highlighted_identifier
                ),
                next((index for index, item in enumerate(items) if not item.disabled), 0),
            )
            if not isinstance(self.focused, Input):
                device_list.focus()

    def action_cursor_down(self) -> None:
        """Move selection down with Vim's ``j`` key."""
        device_list = self.query_one("#device-list", ListView)
        device_list.focus()
        device_list.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move selection up with Vim's ``k`` key."""
        device_list = self.query_one("#device-list", ListView)
        device_list.focus()
        device_list.action_cursor_up()

    async def _discover(self, hosts: tuple[str, ...] | None = None) -> None:
        try:
            await self.controller.discover(
                hosts if hosts is not None else self.hosts,
                self.scan_timeout,
            )
        except Exception:
            # The controller publishes the actionable error for the status panel.
            return

    async def action_discover(self) -> None:
        """Start another discovery scan."""
        manual_host = self.query_one("#manual-host", Input).value.strip()
        hosts = (manual_host,) if manual_host else self.hosts
        self.run_worker(self._discover(hosts), group="discovery", exclusive=True)

    @on(Button.Pressed, "#discover")
    def discover_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_worker(self.action_discover(), group="discovery", exclusive=True)

    @on(Input.Submitted, "#manual-host")
    def manual_host_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.run_worker(self.action_discover(), group="discovery", exclusive=True)

    @on(ListView.Selected, "#device-list")
    def device_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if self._selection_busy or not isinstance(event.item, DeviceListItem):
            return
        device = next(
            (
                candidate
                for candidate in self.controller.state.devices
                if candidate.identifier == event.item.device.identifier
            ),
            event.item.device,
        )
        if device.availability not in _SELECTABLE:
            message = (
                f"Still checking {device.name}…"
                if device.availability is DeviceAvailability.CHECKING
                else f"{device.name} was not found in the latest scan"
            )
            self.query_one("#picker-error", Static).update(message)
            return
        self._selection_busy = True
        self.remote_app.run_worker(
            self._activate_device(device),
            group="device-selection",
            exclusive=True,
        )

    async def _activate_device(self, device: Device) -> None:
        try:
            await self.remote_app.connect_device(device)
        except Exception:
            # Controller state contains the backend exception; keep the picker usable.
            pass
        finally:
            self._selection_busy = False
