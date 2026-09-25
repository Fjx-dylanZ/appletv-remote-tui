"""Connected remote dashboard and the modal Vim-style keyboard router.

The controller only knows whether a tvOS text field is open for editing. This
screen owns everything keyboard-shaped on top of that: REMOTE key bindings,
the TEXT NORMAL / TEXT INSERT editor modes, the escape ladder, debounced
synchronization, and the busy states that drop type-ahead while the Apple TV
is loading or confirming text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Input, Label, Static

from appletv_remote_tui.core import (
    AppState,
    Capabilities,
    ConnectionStatus,
    ControllerError,
    KeyboardFocus,
    RemoteAction,
    RemoteController,
)
from appletv_remote_tui.tui.help import HelpScreen
from appletv_remote_tui.tui.screen import ControllerScreen
from appletv_remote_tui.tui.vim_input import VimInput, VimInputMode

_REMOTE_KEYS = (
    "h j k l / arrows  navigate    Enter  select    ;  hold next    S  hold-select\n"
    "H J K L  swipe    Space / p  play-pause    g  home    + / -  volume\n"
    "Esc  back    i  edit text    d  devices    r  reconnect    ?  help    q  quit"
)


class RemoteScreen(ControllerScreen):
    """The connected remote dashboard and modal Vim-style input router."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h,left", "remote('left')", "Left", show=False),
        Binding("j,down", "remote('down')", "Down", show=False),
        Binding("k,up", "remote('up')", "Up", show=False),
        Binding("l,right", "remote('right')", "Right", show=False),
        Binding("H", "remote('swipe_left')", "Swipe left", show=False),
        Binding("J", "remote('swipe_down')", "Swipe down", show=False),
        Binding("K", "remote('swipe_up')", "Swipe up", show=False),
        Binding("L", "remote('swipe_right')", "Swipe right", show=False),
        Binding("enter", "remote('select')", "Select", show=True),
        Binding("S", "remote('select', True)", "Hold Select", show=True),
        Binding("semicolon", "hold_prefix", "Hold next", show=False),
        Binding("escape", "back_or_leave", "Back / NORMAL", show=True, priority=True),
        Binding("backspace", "remote('menu')", "Back", show=False),
        Binding("space,p", "remote('play_pause')", "Play / Pause", show=True),
        Binding("g", "remote('home')", "Home", show=True),
        Binding("plus", "remote('volume_up')", "Volume +", show=False),
        Binding("minus", "remote('volume_down')", "Volume -", show=False),
        Binding("o", "remote('power_on')", "Power on", show=False),
        Binding("x", "remote('power_off')", "Power off", show=False),
        Binding("i", "insert_mode", "INSERT", show=True),
        Binding("d", "devices", "Devices", show=True),
        Binding("r", "reconnect", "Reconnect", show=False),
        Binding("question_mark", "show_help", "Help", show=True),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    CSS = """
    RemoteScreen {
        align: center middle;
    }

    #remote-shell {
        width: 78;
        max-width: 96%;
        height: auto;
        border: round $accent;
        padding: 0 2;
        background: $surface;
    }

    #remote-heading {
        height: auto;
    }

    #device-name {
        width: 1fr;
        text-style: bold;
    }

    .badge {
        width: auto;
        margin-left: 1;
        padding: 0 1;
        background: $panel;
    }

    #mode-badge.text-insert {
        background: $warning;
        color: $background;
        text-style: bold;
    }

    #mode-badge.text-normal {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    #mode-badge.remote {
        background: $success;
        color: $background;
    }

    #remote-guide {
        height: auto;
        margin-top: 1;
    }

    #remote-keys, #capability-summary {
        height: auto;
    }

    #capability-summary {
        color: $text-muted;
        margin-top: 1;
    }

    #text-panel {
        height: auto;
        margin-top: 1;
    }

    #text-heading {
        height: 1;
        color: $text-muted;
    }

    #text-input {
        margin-top: 0;
    }

    #remote-status {
        height: auto;
        min-height: 1;
        margin-top: 1;
        color: $text-muted;
    }

    #remote-error {
        height: auto;
        min-height: 1;
        color: $error;
    }

    #key-summary {
        height: auto;
        color: $text-disabled;
    }
    """

    def __init__(self, controller: RemoteController, *, text_debounce: float = 0.25) -> None:
        super().__init__(controller)
        self.text_debounce = text_debounce
        self._text_generation = 0
        self._text_tasks: set[asyncio.Task[None]] = set()
        self._text_lock = asyncio.Lock()
        self._was_editing = controller.state.text_editing
        self._submitting = False
        self._loading_text = False
        self._hold_pending = False
        self._command_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Container(id="remote-shell"):
            with Horizontal(id="remote-heading"):
                yield Label("Apple TV", id="device-name", markup=False)
                yield Static("DISCONNECTED", id="connection-badge", classes="badge")
                yield Static("REMOTE", id="mode-badge", classes="badge remote")
            with Vertical(id="remote-guide"):
                yield Static(_REMOTE_KEYS, id="remote-keys", markup=False)
                yield Static("Available: checking…", id="capability-summary", markup=False)
            with Vertical(id="text-panel"):
                yield Static("Keyboard: unavailable · text synchronized", id="text-heading")
                yield VimInput(
                    placeholder="Focus a text field on Apple TV, then press i",
                    id="text-input",
                    disabled=True,
                )
            yield Static("Ready", id="remote-status", markup=False)
            yield Static("", id="remote-error", markup=False)
            yield Static(
                "REMOTE · press i to edit the focused Apple TV text field",
                id="key-summary",
                markup=False,
            )

    def on_mount(self) -> None:
        self.update_state(self.controller.state)
        self.set_focus(None)

    def on_unmount(self) -> None:
        self._hold_pending = False
        self._text_generation += 1
        self._loading_text = False
        for task in self._text_tasks:
            task.cancel()
        self._text_tasks.clear()
        if self._command_task is not None:
            # The adapter still sends the press's release sequence; only this
            # screen's interest in the outcome ends here.
            self._command_task.cancel()
            self._command_task = None

    def on_screen_suspend(self) -> None:
        self._clear_hold()

    def on_key(self, event: events.Key) -> None:
        """Keep HOLD only for bindings that consume it through normal dispatch."""
        if not self._hold_pending:
            return
        binding = self.active_bindings.get(event.key)
        if (
            binding is None
            or binding.node is not self
            or not (
                binding.binding.action.startswith("remote(")
                or binding.binding.action in {"hold_prefix", "back_or_leave"}
            )
        ):
            self._clear_hold()

    def _clear_hold(self) -> bool:
        pending = self._hold_pending
        self._hold_pending = False
        if pending and self.is_mounted:
            self._render_mode(self.controller.state, self.query_one("#text-input", VimInput))
        return pending

    def action_hold_prefix(self) -> None:
        """Toggle a one-shot, fixed-duration hold for the next remote action."""
        state = self.controller.state
        if (
            state.text_editing
            or self._loading_text
            or self._submitting
            or state.connection is not ConnectionStatus.CONNECTED
        ):
            return
        self._hold_pending = not self._hold_pending
        self._render_mode(state, self.query_one("#text-input", VimInput))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable REMOTE bindings while the editor owns keyboard input."""
        if action == "quit_app" and not self.controller.state.text_editing:
            return True
        if self._submitting or self._loading_text:
            return None
        if self.controller.state.text_editing and action != "back_or_leave":
            return None
        return True

    def update_state(self, state: AppState) -> None:
        if not self.is_mounted:
            return
        if state.text_editing or state.connection is not ConnectionStatus.CONNECTED:
            self._hold_pending = False

        device_name = (
            f"{state.current_device.name} · {state.current_device.address}"
            if state.current_device
            else "Apple TV"
        )
        self.query_one("#device-name", Label).update(device_name)
        self.query_one("#connection-badge", Static).update(state.connection.value.upper())

        focus_label = {
            KeyboardFocus.FOCUSED: "focused",
            KeyboardFocus.UNFOCUSED: "not focused",
            KeyboardFocus.UNKNOWN: "unknown",
        }[state.keyboard_focus]
        sync_label = "synchronized" if state.text_synced else "unsynchronized"
        self.query_one("#text-heading", Static).update(
            f"Keyboard: {focus_label} · text {sync_label}"
        )
        self.query_one("#remote-status", Static).update(state.message)
        self.query_one("#remote-error", Static).update(state.error or "")

        text_input = self.query_one("#text-input", VimInput)
        text_input.disabled = not state.text_editing or self._submitting or self._loading_text
        if (
            state.text_editing
            and not self._submitting
            and text_input.value != state.text_buffer
            and not text_input.has_focus
        ):
            text_input.value = state.text_buffer

        if not state.text_editing:
            if self._was_editing:
                self._text_generation += 1
            text_input.enter_normal()
            self.set_focus(None)
        self._was_editing = state.text_editing
        self._render_capabilities(state.capabilities, state.connection)
        self._render_mode(state, text_input)
        self.refresh_bindings()

    def _render_capabilities(
        self, capabilities: Capabilities, connection: ConnectionStatus
    ) -> None:
        if connection is not ConnectionStatus.CONNECTED:
            summary = "Available: disconnected"
        else:
            names = [
                label
                for label, available in (
                    ("navigation", capabilities.navigation),
                    ("swipe", capabilities.swipe),
                    ("back", capabilities.menu),
                    ("home", capabilities.home),
                    ("playback", capabilities.play_pause),
                    ("volume", capabilities.volume),
                    ("power", capabilities.power),
                    ("text", capabilities.keyboard),
                )
                if available
            ]
            summary = "Available: " + (" · ".join(names) if names else "none reported")
        self.query_one("#capability-summary", Static).update(summary)

    def _render_mode(self, state: AppState, text_input: VimInput) -> None:
        badge = self.query_one("#mode-badge", Static)
        badge.remove_class("remote", "text-normal", "text-insert")
        summary = self.query_one("#key-summary", Static)
        if self._loading_text:
            badge.update("LOADING TEXT")
            badge.add_class("text-insert")
            summary.update("LOADING TEXT · keyboard paused while Apple TV opens the field")
        elif self._submitting:
            badge.update("SYNCING")
            badge.add_class("text-insert")
            summary.update("SYNCING · keyboard paused until Apple TV confirms the text")
        elif not state.text_editing:
            badge.update("REMOTE · HOLD" if self._hold_pending else "REMOTE")
            badge.add_class("remote")
            summary.update(
                "HOLD · g home · Enter select · h/j/k/l/arrows · Backspace back\n"
                "1 second · Esc / ; cancel · other keys clear HOLD"
                if self._hold_pending
                else "REMOTE · ; hold next (1 second) · i edit TV text"
            )
        elif text_input.vim_mode is VimInputMode.INSERT:
            badge.update("TEXT INSERT")
            badge.add_class("text-insert")
            summary.update("TEXT INSERT · type normally · Esc normal · Enter sync + select")
        else:
            pending = f" · {text_input.pending}" if text_input.pending else ""
            badge.update(f"TEXT NORMAL{pending}")
            badge.add_class("text-normal")
            summary.update("TEXT NORMAL · h/l w/b/e 0/$ · i/a/I/A · dw/cw diw/ciw · Esc remote")

    def _show_local_error(self, message: str) -> None:
        self.query_one("#remote-error", Static).update(message)

    def action_remote(self, action_name: str, hold: bool = False) -> None:
        """Consume HOLD, then start the command unless one is still in flight.

        Sending runs in the background so keys keep being read while the
        Apple TV completes a press. A press that arrives meanwhile, typically
        keyboard auto-repeat, is dropped rather than queued: releasing the key
        leaves no queued repeats behind (the accepted command still finishes),
        and quit never waits behind a backlog.
        """
        prefixed = self._clear_hold()
        if self.controller.state.text_editing or self._loading_text or self._submitting:
            return
        if self._command_task is not None and not self._command_task.done():
            return
        self._command_task = asyncio.create_task(
            self._send_command(RemoteAction(action_name), hold=hold or prefixed)
        )

    async def _send_command(self, action: RemoteAction, *, hold: bool) -> None:
        try:
            await self.controller.send(action, hold=hold)
        except asyncio.CancelledError:
            raise
        except ControllerError as ex:
            if self.is_mounted:
                self._show_local_error(str(ex))
        except Exception as ex:
            if self.is_mounted and not self.controller.state.error:
                self._show_local_error(str(ex))

    async def action_back_or_leave(self) -> None:
        """Walk TEXT INSERT → TEXT NORMAL → REMOTE → Apple TV Back."""
        if self._clear_hold():
            return
        if self.controller.state.text_editing:
            text_input = self.query_one("#text-input", VimInput)
            if text_input.vim_mode is VimInputMode.INSERT:
                text_input.enter_normal()
                self._render_mode(self.controller.state, text_input)
                self.query_one("#remote-status", Static).update("TEXT NORMAL mode")
                return
            if text_input.cancel_pending():
                self._render_mode(self.controller.state, text_input)
                return

            self._begin_text_commit(text_input.value, submit=False)
            return
        self.action_remote(RemoteAction.MENU.value)

    def action_insert_mode(self) -> None:
        """Load the focused tvOS field and focus the local editor."""
        self._clear_hold()
        if self.controller.state.text_editing or self._loading_text:
            return
        self._loading_text = True
        text_input = self.query_one("#text-input", VimInput)
        text_input.disabled = True
        self.query_one("#remote-status", Static).update("Loading Apple TV text…")
        self._render_mode(self.controller.state, text_input)
        self.refresh_bindings()
        self._spawn_text_task(self._load_text_editor())

    async def _load_text_editor(self) -> None:
        """Load remote text in the background while the screen drops type-ahead."""
        text = ""
        error: Exception | None = None
        try:
            text = await self.controller.begin_text_edit()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            error = ex
        finally:
            self._loading_text = False

        if not self.is_mounted:
            return
        if error is not None:
            self.update_state(self.controller.state)
            self._show_local_error(str(error))
            return

        text_input = self.query_one("#text-input", VimInput)
        text_input.disabled = False
        text_input.load_value(text)
        text_input.enter_insert("A")
        text_input.focus()
        self._render_mode(self.controller.state, text_input)

    def action_show_help(self) -> None:
        self._clear_hold()
        self.remote_app.push_screen(HelpScreen())

    async def action_devices(self) -> None:
        self._clear_hold()
        await self.remote_app.show_devices()

    async def action_reconnect(self) -> None:
        self._clear_hold()
        device = self.controller.state.current_device
        if device is None:
            self._show_local_error("No Apple TV is selected")
            return
        try:
            await self.controller.connect(device)
        except Exception:
            # Controller state carries the failure for the status panel.
            return

    @on(VimInput.ModeChanged)
    def vim_mode_changed(self, event: VimInput.ModeChanged) -> None:
        event.stop()
        self._render_mode(self.controller.state, event.input)

    @on(Input.Changed, "#text-input")
    def text_changed(self, event: Input.Changed) -> None:
        if not self.controller.state.text_editing or self._submitting:
            return
        if event.value == self.controller.state.text_buffer:
            return
        try:
            self.controller.edit_text(event.value)
        except ControllerError as ex:
            self._show_local_error(str(ex))
            return
        self._text_generation += 1
        self._spawn_text_task(self._debounced_sync(self._text_generation, event.value))

    async def _debounced_sync(self, generation: int, text: str) -> None:
        try:
            await asyncio.sleep(self.text_debounce)
            if generation != self._text_generation or self._submitting:
                return
            async with self._text_lock:
                if generation != self._text_generation or self._submitting:
                    return
                await self.controller.sync_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Controller state already carries the transport error.
            return

    def _begin_text_commit(self, text: str, *, submit: bool) -> None:
        """Freeze editing and commit text without blocking Textual's input loop."""
        if self._submitting:
            return
        if text != self.controller.state.text_buffer:
            try:
                self.controller.edit_text(text)
            except ControllerError as ex:
                self._show_local_error(str(ex))
                return
        self._submitting = True
        self._text_generation += 1
        text_input = self.query_one("#text-input", VimInput)
        text_input.disabled = True
        self.set_focus(None)
        self.query_one("#remote-status", Static).update("Synchronizing Apple TV text…")
        self._render_mode(self.controller.state, text_input)
        self.refresh_bindings()
        self._spawn_text_task(self._commit_text(text, submit=submit))

    async def _commit_text(self, text: str, *, submit: bool) -> None:
        succeeded = False
        try:
            async with self._text_lock:
                if submit:
                    await self.controller.submit_text(text)
                else:
                    if text != self.controller.state.text_buffer:
                        self.controller.edit_text(text)
                    await self.controller.sync_text(text)
                    self.controller.end_text_edit()
            succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            if self.is_mounted and not self.controller.state.error:
                self._show_local_error(str(ex))
        finally:
            self._submitting = False
            if self.is_mounted:
                self.update_state(self.controller.state)
                if not succeeded and self.controller.state.text_editing:
                    text_input = self.query_one("#text-input", VimInput)
                    text_input.disabled = False
                    text_input.focus()

    @on(Input.Submitted, "#text-input")
    def text_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if not self.controller.state.text_editing or self._submitting:
            return
        self._begin_text_commit(event.value, submit=True)

    def _spawn_text_task(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._text_tasks.add(task)
        task.add_done_callback(self._text_tasks.discard)
