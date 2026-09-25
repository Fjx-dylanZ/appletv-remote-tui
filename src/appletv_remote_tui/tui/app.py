"""Textual application: screen flow on top of the toolkit-neutral controller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ClassVar, cast

from textual.app import App
from textual.await_complete import AwaitComplete
from textual.binding import Binding, BindingType

from appletv_remote_tui.core import AppState, ConnectionStatus, Device, RemoteController
from appletv_remote_tui.tui.device_picker import DevicePickerScreen
from appletv_remote_tui.tui.pairing import PinPairingScreen
from appletv_remote_tui.tui.remote import RemoteScreen
from appletv_remote_tui.tui.screen import ControllerScreen


class RemoteTuiApp(App[None]):
    """A Vim-friendly Apple TV remote built around a toolkit-neutral controller."""

    TITLE = "appletv-remote-tui"
    SUB_TITLE = "Apple TV remote"
    ESCAPE_TO_MINIMIZE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit_app", "Quit", priority=True, show=False),
    ]

    def __init__(
        self,
        controller: RemoteController,
        *,
        hosts: tuple[str, ...] | None = None,
        scan_timeout: float = 5.0,
        text_debounce: float = 0.25,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.hosts = hosts
        self.scan_timeout = scan_timeout
        self.text_debounce = text_debounce
        self._close_task: asyncio.Task[None] | None = None
        self._latest_state = controller.state

    async def on_mount(self) -> None:
        self.controller.add_listener(self._controller_state_changed)
        await self.push_screen(self._device_picker(auto_discover=True))

    async def on_unmount(self) -> None:
        self.controller.remove_listener(self._controller_state_changed)
        await self._close_controller()

    def _controller_state_changed(self, state: AppState) -> None:
        self._latest_state = state
        if self.is_running:
            self.call_later(self._publish_state)

    def _publish_state(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, ControllerScreen):
                screen.update_state(self._latest_state)

    def _device_picker(self, *, auto_discover: bool) -> DevicePickerScreen:
        return DevicePickerScreen(
            self.controller,
            hosts=self.hosts,
            scan_timeout=self.scan_timeout,
            auto_discover=auto_discover,
        )

    def _remote_screen(self) -> RemoteScreen:
        return RemoteScreen(self.controller, text_debounce=self.text_debounce)

    def _switch_to(self, screen: ControllerScreen) -> None:
        # Textual annotates ``switch_screen`` with a bare ``Screen``; pin the
        # result type here so callers stay fully typed.
        switch = cast("Callable[[ControllerScreen], AwaitComplete]", self.switch_screen)
        switch(screen)

    async def connect_device(self, device: Device) -> None:
        """Pair when necessary, connect, then open the remote dashboard."""
        state = self.controller.state
        if (
            state.connection is ConnectionStatus.CONNECTED
            and state.current_device is not None
            and state.current_device.identifier == device.identifier
        ):
            self._switch_to(self._remote_screen())
            return
        if not device.paired:
            device_provides_pin = await self.controller.begin_pairing(device)
            pin = await self.push_screen(
                PinPairingScreen(device, device_provides_pin=device_provides_pin),
                wait_for_dismiss=True,
            )
            if pin is None:
                await self.controller.close()
                return
            await self.controller.finish_pairing(pin)
            if self.controller.state.current_device is not None:
                device = self.controller.state.current_device

        await self.controller.connect(device)
        # This method runs in a worker owned by the picker. Awaiting removal of
        # that worker's own screen would deadlock; scheduling the switch lets the
        # worker finish before Textual tears the picker down.
        self._switch_to(self._remote_screen())

    async def show_devices(self) -> None:
        """Return to discovery while keeping the current connection available."""
        self._switch_to(self._device_picker(auto_discover=False))

    async def _close_controller(self) -> None:
        """Close the controller once; every caller waits for that same close."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self.controller.close())
        await asyncio.shield(self._close_task)

    async def shutdown(self) -> None:
        """Release backend resources and exit Textual exactly once."""
        await self._close_controller()
        self.exit()

    async def action_quit_app(self) -> None:
        await self.shutdown()
