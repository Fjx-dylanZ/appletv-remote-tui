"""One-shot hold routing through Textual's real binding dispatch."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from appletv_remote_tui.core import Device, RemoteAction
from appletv_remote_tui.tui import VimInput
from tests.tui.test_app import connect_first_device, make_app


async def test_prefix_survives_binding_dispatch_and_is_consumed_before_send() -> None:
    app, backend = make_app([Device("tv", "TV", "10.0.0.1", paired=True)])
    backend.session.first_send.enabled = True
    async with app.run_test(size=(80, 24)) as pilot:
        await connect_first_device(pilot)
        badge = app.screen.query_one("#mode-badge", Static)
        original_badge = str(badge.content)
        await pilot.press("semicolon")
        assert str(badge.content) != original_badge
        summary = app.screen.query_one("#key-summary", Static)
        assert summary.region.bottom <= app.screen.region.bottom
        assert summary.region.right <= app.screen.region.right
        # A binding refresh must not consume the prefix (check_action is pure).
        app.screen.refresh_bindings()
        sending = asyncio.create_task(pilot.press("g"))
        try:
            await asyncio.wait_for(backend.session.first_send.started.wait(), timeout=1)
            assert str(badge.content) == original_badge
        finally:
            backend.session.first_send.release.set()
            await sending
        await pilot.press("g", "semicolon", "enter", "semicolon", "backspace")
        assert backend.session.actions == [
            RemoteAction.HOME,
            RemoteAction.HOME,
            RemoteAction.SELECT,
            RemoteAction.MENU,
        ]
        assert backend.session.holds == [
            RemoteAction.HOME,
            RemoteAction.SELECT,
            RemoteAction.MENU,
        ]


@pytest.mark.parametrize("cancel", ["escape", "semicolon", "z", "tab"])
async def test_cancel_never_sends_back_or_leaves_hold_latched(cancel: str) -> None:
    app, backend = make_app([Device("tv", "TV", "10.0.0.1", paired=True)])
    async with app.run_test() as pilot:
        await connect_first_device(pilot)
        await pilot.press("semicolon", cancel)
        assert backend.session.actions == []
        await pilot.press("h")
        assert backend.session.actions == [RemoteAction.LEFT]
        assert backend.session.holds == []


async def test_unsupported_hold_is_consumed_without_tap_fallback() -> None:
    app, backend = make_app([Device("tv", "TV", "10.0.0.1", paired=True)])
    async with app.run_test() as pilot:
        await connect_first_device(pilot)
        await pilot.press("semicolon", "p")
        assert backend.session.actions == []
        await pilot.press("enter")
        assert backend.session.actions == [RemoteAction.SELECT]
        assert backend.session.holds == []


@pytest.mark.parametrize("transition", ["help", "devices", "reconnect", "disconnect"])
async def test_hold_is_cleared_across_screen_and_connection_transitions(transition: str) -> None:
    app, backend = make_app([Device("tv", "TV", "10.0.0.1", paired=True)])
    async with app.run_test() as pilot:
        await connect_first_device(pilot)
        await pilot.press("semicolon")
        if transition == "help":
            await pilot.press("question_mark", "escape")
        elif transition == "devices":
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")
        elif transition == "reconnect":
            await pilot.press("r")
        else:
            backend.disconnect(None)
            await pilot.pause()
            await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        assert backend.session.actions == [RemoteAction.SELECT]
        assert backend.session.holds == []


async def test_text_modes_own_semicolon_and_clear_remote_prefix() -> None:
    app, backend = make_app([Device("tv", "TV", "10.0.0.1", paired=True)])
    async with app.run_test() as pilot:
        await connect_first_device(pilot)
        await pilot.press("semicolon", "i", "semicolon", "g")
        assert app.screen.query_one("#text-input", VimInput).value == ";g"
        await pilot.press("escape", "semicolon", "escape", "enter")
        assert backend.session.text == ";g"
        assert backend.session.actions == [RemoteAction.SELECT]
        assert backend.session.holds == []


@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_quit_remains_available_with_hold_pending(key: str) -> None:
    app, backend = make_app([Device("tv", "TV", "10.0.0.1", paired=True)])
    async with app.run_test() as pilot:
        await connect_first_device(pilot)
        await pilot.press("semicolon", key)
    assert not app.is_running
    assert backend.session.actions == []
