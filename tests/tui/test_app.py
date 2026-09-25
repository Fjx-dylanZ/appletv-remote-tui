"""Headless interaction tests for the Textual application."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import ListView, Static

from appletv_remote_tui.core import (
    Capabilities,
    ControllerError,
    Device,
    DeviceAvailability,
    KeyboardFocus,
    RemoteAction,
    RemoteController,
)
from appletv_remote_tui.tui import DeviceListItem, RemoteTuiApp, VimInput, VimInputMode
from tests.fakes import FakeBackend, FakeDeviceHistory


def make_app(
    devices: list[Device],
    *,
    text: str = "",
    hosts: tuple[str, ...] | None = None,
    text_debounce: float = 0.01,
    history: FakeDeviceHistory | None = None,
) -> tuple[RemoteTuiApp, FakeBackend]:
    backend = FakeBackend(devices, text=text)
    app = RemoteTuiApp(
        RemoteController(backend, history=history),
        hosts=hosts,
        scan_timeout=0.01,
        text_debounce=text_debounce,
    )
    return app, backend


async def connect_first_device(pilot: Pilot[None]) -> None:
    await pilot.pause(0.08)
    await pilot.press("enter")
    await pilot.pause(0.12)


async def test_device_picker_supports_vim_selection_and_repeatable_host_filter() -> None:
    bedroom = Device("bedroom", "Bedroom", "10.0.0.2", paired=True)
    living_room = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([living_room, bedroom], hosts=("10.0.0.2", "10.0.0.3"))

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause(0.08)
        device_list = app.screen.query_one("#device-list", ListView)
        assert device_list.index == 0
        assert backend.discover_calls[-1][0] == ("10.0.0.2", "10.0.0.3")

        await pilot.press("j")
        assert device_list.index == 1
        await pilot.press("k")
        assert device_list.index == 0

        await pilot.press("enter")
        await pilot.pause(0.12)
        assert type(app.screen).__name__ == "RemoteScreen"
        assert backend.connect_calls[-1] == bedroom


async def test_device_picker_renders_and_verifies_remembered_devices_during_scan() -> None:
    recent = Device(
        "living",
        "Old Living Room",
        "10.0.0.3",
        paired=True,
        last_connected_at=datetime(2026, 7, 18, 13, tzinfo=UTC),
    )
    older = Device(
        "bedroom",
        "Bedroom",
        "10.0.0.2",
        paired=True,
        last_connected_at=datetime(2026, 7, 17, 13, tzinfo=UTC),
    )
    refreshed = Device(
        "living",
        "Living Room",
        "10.0.0.33",
        model="Apple TV 4K",
        os_version="18.5",
        paired=True,
    )
    new_device = Device("den", "Den", "10.0.0.4", paired=True)
    history = FakeDeviceHistory((older, recent))
    app, backend = make_app([new_device, refreshed], history=history)
    backend.discovery.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        try:
            await asyncio.wait_for(backend.discovery.started.wait(), timeout=1)
            await pilot.pause(0.05)

            checking_rows = list(app.screen.query(DeviceListItem))
            assert [row.device.identifier for row in checking_rows] == ["living", "bedroom"]
            assert all(
                row.device.availability is DeviceAvailability.CHECKING for row in checking_rows
            )
            assert all(row.disabled for row in checking_rows)

            # Remembered metadata is visible immediately, but cannot be used to
            # connect until discovery has supplied a fresh backend configuration.
            await pilot.press("enter")
            await pilot.pause(0.03)
            assert backend.connect_calls == []
            assert type(app.screen).__name__ == "DevicePickerScreen"

            device_list = app.screen.query_one("#device-list", ListView)
            device_list.index = 1
            assert isinstance(device_list.highlighted_child, DeviceListItem)
            assert device_list.highlighted_child.device.identifier == "bedroom"

            backend.discovery.release.set()
            await pilot.pause(0.1)

            verified_rows = list(app.screen.query(DeviceListItem))
            assert [row.device.identifier for row in verified_rows] == [
                "living",
                "bedroom",
                "den",
            ]
            assert verified_rows[0].device.name == "Living Room"
            assert verified_rows[0].device.address == "10.0.0.33"
            assert verified_rows[0].device.model == "Apple TV 4K"
            assert verified_rows[0].device.availability is DeviceAvailability.AVAILABLE
            assert verified_rows[1].device.availability is DeviceAvailability.UNAVAILABLE
            assert verified_rows[2].device.availability is DeviceAvailability.AVAILABLE
            assert [row.disabled for row in verified_rows] == [False, True, False]

            assert isinstance(device_list.highlighted_child, DeviceListItem)
            assert device_list.highlighted_child.device.identifier == "bedroom"
            assert history.saved[-1][0].address == "10.0.0.33"
        finally:
            backend.discovery.release.set()


async def test_pairing_pin_modal_connects_an_unpaired_device() -> None:
    device = Device("living", "Living Room", "10.0.0.3")
    app, backend = make_app([device])

    async with app.run_test(size=(100, 42)) as pilot:
        await pilot.pause(0.08)
        await pilot.press("enter")
        await pilot.pause(0.08)
        assert type(app.screen).__name__ == "PinPairingScreen"

        await pilot.press("1", "2", "3", "4", "enter")
        await pilot.pause(0.15)
        assert backend.pairing.finish_calls == ["1234"]
        assert backend.pairing.close_calls == 1
        assert type(app.screen).__name__ == "RemoteScreen"


async def test_remote_mode_routes_vim_keys_and_enter_to_remote() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        assert app.focused is None

        await pilot.press("h", "j", "k", "l", "enter")
        await pilot.pause(0.05)

        assert backend.session.actions == [
            RemoteAction.LEFT,
            RemoteAction.DOWN,
            RemoteAction.UP,
            RemoteAction.RIGHT,
            RemoteAction.SELECT,
        ]


async def test_uppercase_keys_send_hold_select_and_swipes() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)

        await pilot.press("S", "H", "J", "K", "L")
        await pilot.pause(0.05)

        assert backend.session.actions == [
            RemoteAction.SELECT,
            RemoteAction.SWIPE_LEFT,
            RemoteAction.SWIPE_DOWN,
            RemoteAction.SWIPE_UP,
            RemoteAction.SWIPE_RIGHT,
        ]
        assert backend.session.holds == [RemoteAction.SELECT]


async def test_unsupported_action_is_reported_locally_without_sending() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])
    backend.session.capabilities = Capabilities(swipe=False)

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)

        await pilot.press("H")
        await pilot.pause(0.05)

        assert backend.session.actions == []
        with pytest.raises(ControllerError) as refused:
            await app.controller.send(RemoteAction.SWIPE_LEFT)
        assert str(app.screen.query_one("#remote-error", Static).content) == str(refused.value)


async def test_presses_during_an_in_flight_command_are_dropped_not_queued() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])
    backend.session.first_send.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        badge = app.screen.query_one("#mode-badge", Static)
        idle_badge = str(badge.content)
        try:
            # Bounded: an inline-awaited send would block the pilot here.
            async with asyncio.timeout(2):
                await pilot.press("h")
                await backend.session.first_send.started.wait()
                # Auto-repeat and a prefixed press arrive while the first press
                # is still with the Apple TV: nothing is queued, the prefix is spent.
                await pilot.press("h", "h", "semicolon", "h", "j")
                assert str(badge.content) == idle_badge
                assert backend.session.events == ["send-start:left"]
        finally:
            backend.session.first_send.release.set()
        await pilot.pause(0.05)
        await pilot.press("l")
        await pilot.pause(0.05)

        assert backend.session.actions == [RemoteAction.LEFT, RemoteAction.RIGHT]
        assert backend.session.holds == []


@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_quit_is_honoured_while_a_command_is_still_with_the_apple_tv(key: str) -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])
    backend.session.first_send.enabled = True
    backend.session.close_gate.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        quitting: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(2):
                await pilot.press("S")
                await backend.session.first_send.started.wait()
                quitting = asyncio.create_task(pilot.press("S", "S", key))
                # Shutdown reaches the session while its first press is still pending.
                await backend.session.close_gate.started.wait()
                assert backend.session.events == ["send-start:select:hold"]
        finally:
            backend.session.close_gate.release.set()
            backend.session.first_send.release.set()
            if quitting is not None:
                await quitting

    assert not app.is_running
    assert backend.session.close_calls == 1
    started = [event for event in backend.session.events if event.startswith("send-start:")]
    assert started == ["send-start:select:hold"]


async def test_concurrent_shutdowns_share_one_close_and_none_exits_early() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])
    backend.session.close_gate.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        shutdowns: list[asyncio.Task[None]] = []
        try:
            async with asyncio.timeout(2):
                shutdowns.append(asyncio.create_task(app.shutdown()))
                await backend.session.close_gate.started.wait()
                shutdowns.append(asyncio.create_task(app.shutdown()))
                await pilot.pause(0.05)
                assert not any(shutdown.done() for shutdown in shutdowns)
                assert app.is_running
                assert backend.session.close_calls == 1
        finally:
            backend.session.close_gate.release.set()
            await asyncio.gather(*shutdowns, return_exceptions=True)

    assert not app.is_running
    assert backend.session.close_calls == 1


async def test_insert_mode_owns_keys_and_orders_final_sync_before_select() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="find ", text_debounce=1.0)

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "h", "j", "q", "S", "H", "L")
        await pilot.pause(0.04)

        text_input = app.screen.query_one("#text-input", VimInput)
        assert text_input.value == "find hjqSHL"
        assert backend.session.actions == []
        assert app.is_running

        backend.session.events.clear()
        await pilot.press("enter")
        await pilot.pause(0.08)

        assert app.controller.state.text_editing is False
        assert backend.session.events == [
            "set-text-start:find hjqSHL",
            "set-text-end:find hjqSHL",
            "send-start:select",
            "send-end:select",
        ]


async def test_slow_text_load_shows_busy_mode_and_suppresses_remote_keys() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="query")
    backend.session.get_text_gate.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i")
        await asyncio.wait_for(backend.session.get_text_gate.started.wait(), timeout=1)

        mode_badge = app.screen.query_one("#mode-badge", Static)
        try:
            assert "LOADING TEXT" in str(mode_badge.content)
            assert app.screen.check_action("remote", ("left",)) is None
            await pilot.press("semicolon", "h", "x")
            assert backend.session.actions == []
        finally:
            backend.session.get_text_gate.release.set()
        await pilot.pause()
        text_input = app.screen.query_one("#text-input", VimInput)
        assert text_input.vim_mode is VimInputMode.INSERT
        assert text_input.value == "query"


async def test_failed_text_load_drops_queued_remote_keys() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="stale")
    backend.session.get_text_gate.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i")
        await asyncio.wait_for(backend.session.get_text_gate.started.wait(), timeout=1)

        await pilot.press("semicolon", "h", "x")
        backend.set_keyboard_focus(KeyboardFocus.UNFOCUSED)
        backend.session.get_text_gate.release.set()
        await pilot.pause(0.08)

        assert backend.session.actions == []
        assert app.controller.state.text_editing is False
        await pilot.press("enter")
        assert backend.session.holds == []


async def test_final_submit_freezes_editor_and_drops_keys_typed_during_sync() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text_debounce=1.0)
    backend.session.set_text_gate.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "a", "enter")
        await asyncio.wait_for(backend.session.set_text_gate.started.wait(), timeout=1)

        text_input = app.screen.query_one("#text-input", VimInput)
        assert text_input.disabled
        await pilot.press("semicolon", "h", "x")
        assert text_input.value == "a"
        assert backend.session.actions == []

        backend.session.set_text_gate.release.set()
        await pilot.pause(0.1)
        assert app.controller.state.text_editing is False
        assert backend.session.text == "a"
        assert backend.session.actions == [RemoteAction.SELECT]
        await pilot.press("h")
        assert backend.session.holds == []


async def test_final_escape_sync_drops_remote_commands_typed_while_waiting() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="alpha", text_debounce=1.0)
    backend.session.set_text_gate.enabled = True

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "escape", "x", "escape")
        await asyncio.wait_for(backend.session.set_text_gate.started.wait(), timeout=1)

        await pilot.press("h", "x")
        assert backend.session.actions == []

        backend.session.set_text_gate.release.set()
        await pilot.pause(0.1)
        assert app.controller.state.text_editing is False
        assert backend.session.text == "alph"
        assert backend.session.actions == []


async def test_escape_walks_text_insert_to_text_normal_to_remote_to_back() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device])

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "escape")
        await pilot.pause(0.04)

        text_input = app.screen.query_one("#text-input", VimInput)
        assert app.controller.state.text_editing is True
        assert text_input.vim_mode is VimInputMode.NORMAL
        assert backend.session.actions == []

        await pilot.press("escape")
        await pilot.pause(0.04)
        assert app.controller.state.text_editing is False
        assert backend.session.actions == []

        await pilot.press("escape")
        await pilot.pause(0.04)
        assert backend.session.actions == [RemoteAction.MENU]


async def test_text_normal_diw_and_ciw_edit_without_leaking_remote_keys() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="alpha beta gamma", text_debounce=1.0)

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "escape", "0", "w", "d", "i", "w")
        await pilot.pause(0.04)

        text_input = app.screen.query_one("#text-input", VimInput)
        assert text_input.vim_mode is VimInputMode.NORMAL
        assert text_input.value == "alpha  gamma"
        assert backend.session.actions == []

        await pilot.press("w", "c", "i", "w", "n", "e", "w", "escape")
        assert text_input.vim_mode is VimInputMode.NORMAL
        assert text_input.value == "alpha  new"

        await pilot.press("H", "J", "K", "L")
        assert text_input.value == "alpha  new"
        assert backend.session.actions == []

        backend.session.events.clear()
        await pilot.press("enter")
        await pilot.pause(0.08)
        assert app.controller.state.text_editing is False
        assert backend.session.text == "alpha  new"
        assert backend.session.actions == [RemoteAction.SELECT]


async def test_escape_cancels_pending_text_operator_before_leaving_text_mode() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="alpha beta")

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "escape", "d")
        text_input = app.screen.query_one("#text-input", VimInput)
        assert text_input.pending == "d"

        await pilot.press("escape")
        assert text_input.pending == ""
        assert app.controller.state.text_editing is True
        assert backend.session.actions == []

        await pilot.press("escape")
        await pilot.pause(0.04)
        assert app.controller.state.text_editing is False


async def test_new_remote_text_field_starts_with_fresh_undo_history() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, backend = make_app([device], text="one", text_debounce=1.0)

    async with app.run_test(size=(100, 42)) as pilot:
        await connect_first_device(pilot)
        await pilot.press("i", "X", "enter")
        await pilot.pause(0.08)
        assert backend.session.text == "oneX"

        backend.session.text = "two"
        await pilot.press("i", "escape", "u")

        text_input = app.screen.query_one("#text-input", VimInput)
        assert text_input.vim_mode is VimInputMode.NORMAL
        assert text_input.value == "two"


async def test_remote_and_help_fit_or_scroll_at_standard_terminal_size() -> None:
    device = Device("living", "Living Room", "10.0.0.3", paired=True)
    app, _ = make_app([device])

    async with app.run_test(size=(80, 24)) as pilot:
        await connect_first_device(pilot)
        shell = app.screen.query_one("#remote-shell")
        key_summary = app.screen.query_one("#key-summary")
        assert shell.region.bottom <= app.screen.region.bottom
        assert key_summary.region.bottom <= app.screen.region.bottom

        await pilot.press("question_mark")
        help_copy = app.screen.query_one("#help-copy", VerticalScroll)
        assert help_copy.max_scroll_y > 0
        await pilot.press("end")
        await pilot.pause()
        assert help_copy.scroll_y == help_copy.max_scroll_y
