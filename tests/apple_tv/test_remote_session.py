from __future__ import annotations

import asyncio
import warnings
from collections.abc import Awaitable, Callable, Mapping
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock

import pytest
from pyatv import exceptions as pyatv_exceptions
from pyatv import support
from pyatv.const import FeatureName, FeatureState, InputAction, KeyboardFocusState, TouchAction
from pyatv.core import Core
from pyatv.interface import AppleTV, DeviceListener
from pyatv.protocols.companion import CompanionTouchGestures
from pyatv.protocols.companion.api import CompanionAPI
from pyatv.support.opack import pack

from appletv_remote_tui.apple_tv import PyatvRemoteSession
from appletv_remote_tui.core.controller import RemoteController
from appletv_remote_tui.core.models import Device, KeyboardFocus, RemoteAction
from appletv_remote_tui.core.ports import AppleTVBackend


class AvailableFeatures:
    def __init__(self, unavailable: set[FeatureName] | None = None) -> None:
        self.unavailable = unavailable or set()

    def in_state(self, state: FeatureState, *features: FeatureName) -> bool:
        return state is FeatureState.Available and not self.unavailable.intersection(features)


class FakeAppleTV:
    touch: CompanionTouchGestures

    def __init__(self) -> None:
        self.listener: DeviceListener | None = None
        self.features = AvailableFeatures()
        self.remote_control = SimpleNamespace(
            up=AsyncMock(),
            down=AsyncMock(),
            left=AsyncMock(),
            right=AsyncMock(),
            select=AsyncMock(),
            menu=AsyncMock(),
            home=AsyncMock(),
            play_pause=AsyncMock(),
            volume_up=AsyncMock(),
            volume_down=AsyncMock(),
        )
        self.audio = SimpleNamespace(volume_up=AsyncMock(), volume_down=AsyncMock())
        self.power = SimpleNamespace(turn_on=AsyncMock(), turn_off=AsyncMock())
        self.keyboard = SimpleNamespace(
            listener=None,
            text_focus_state=KeyboardFocusState.Unfocused,
            text_get=AsyncMock(return_value="current text"),
            text_set=AsyncMock(),
        )
        self.close_calls = 0

    def close(self) -> set[asyncio.Task[None]]:
        self.close_calls += 1
        return set()


@pytest.mark.asyncio
async def test_keyboard_focus_updates_notify_consumers() -> None:
    atv = FakeAppleTV()
    focus_updates: list[KeyboardFocus] = []
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=lambda error: None,
        on_keyboard_focus=focus_updates.append,
    )

    assert session.keyboard_focus is KeyboardFocus.UNFOCUSED
    atv.keyboard.listener.focusstate_update(
        KeyboardFocusState.Unfocused,
        KeyboardFocusState.Focused,
    )
    assert session.keyboard_focus is KeyboardFocus.FOCUSED
    assert focus_updates == [KeyboardFocus.FOCUSED]

    await session.close()
    assert atv.close_calls == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_next_command_after_hold_release() -> None:
    atv = FakeAppleTV()
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    async def held_select(action: InputAction) -> None:
        assert action is InputAction.Hold
        events.append("down")
        started.set()
        await release.wait()
        events.append("up")

    async def right(action: InputAction = InputAction.SingleTap) -> None:
        assert action is InputAction.SingleTap
        events.append("right")

    atv.remote_control.select = held_select
    atv.remote_control.right = right
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=lambda error: None,
        on_keyboard_focus=lambda focus: None,
    )
    device = Device("test", "Test", "192.0.2.1", paired=True)
    backend = SimpleNamespace(
        discover=AsyncMock(return_value=[device]),
        connect=AsyncMock(return_value=session),
    )
    controller = RemoteController(cast(AppleTVBackend, cast(object, backend)))
    await controller.discover()
    await controller.connect(device)
    sending = asyncio.create_task(controller.send(RemoteAction.SELECT, hold=True))
    await started.wait()
    queued = asyncio.create_task(controller.send(RemoteAction.RIGHT))
    await asyncio.sleep(0)
    try:
        sending.cancel()
        await asyncio.sleep(0)
        sending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not sending.done()
        assert not queued.done()
        assert events == ["down"]
    finally:
        release.set()
        await asyncio.gather(sending, queued, return_exceptions=True)
        await session.close()

    assert sending.cancelled()
    assert events == ["down", "up", "right"]


@pytest.mark.asyncio
async def test_close_refuses_new_taps_and_waits_for_the_in_flight_up() -> None:
    atv = FakeAppleTV()
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    async def select(action: InputAction = InputAction.SingleTap) -> None:
        assert action is InputAction.SingleTap
        events.append("down")
        started.set()
        await release.wait()
        events.append("up")

    atv.remote_control.select = select
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=lambda error: None,
        on_keyboard_focus=lambda focus: None,
    )
    tapping = asyncio.create_task(session.send(RemoteAction.SELECT))
    closing: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(2):
            await started.wait()
            tapping.cancel()
            closing = asyncio.create_task(session.close())
            await asyncio.sleep(0)
            with pytest.raises(pyatv_exceptions.InvalidStateError):
                await session.send(RemoteAction.RIGHT)
            await asyncio.sleep(0)
            assert not closing.done()
            assert atv.close_calls == 0
            assert events == ["down"]
    finally:
        release.set()
        pending = [task for task in (tapping, closing) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)

    assert tapping.cancelled()
    assert events == ["down", "up"]
    assert atv.close_calls == 1
    atv.remote_control.right.assert_not_awaited()


@pytest.mark.asyncio
async def test_press_failing_on_the_wire_disconnects_even_if_the_caller_was_cancelled() -> None:
    atv = FakeAppleTV()
    started = asyncio.Event()
    fail = asyncio.Event()
    disconnects: list[str | None] = []

    async def select(action: InputAction = InputAction.SingleTap) -> None:
        started.set()
        await fail.wait()
        raise pyatv_exceptions.ProtocolError("Command _hidC failed")

    atv.remote_control.select = select
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=disconnects.append,
        on_keyboard_focus=lambda focus: None,
    )
    tapping = asyncio.create_task(session.send(RemoteAction.SELECT))
    try:
        async with asyncio.timeout(2):
            await started.wait()
            tapping.cancel()
    finally:
        fail.set()
        await asyncio.gather(tapping, return_exceptions=True)

    assert tapping.cancelled()
    assert disconnects == ["Command _hidC failed"]
    with pytest.raises(pyatv_exceptions.InvalidStateError):
        await session.send(RemoteAction.RIGHT)
    await session.close()
    assert atv.close_calls == 1
    atv.remote_control.right.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_cancelled_underneath_an_uncancelled_caller_fails_closed() -> None:
    atv = FakeAppleTV()
    disconnects: list[str | None] = []

    async def select(action: InputAction = InputAction.SingleTap) -> None:
        raise asyncio.CancelledError

    atv.remote_control.select = select
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=disconnects.append,
        on_keyboard_focus=lambda focus: None,
    )

    with pytest.raises(pyatv_exceptions.CommandError) as interrupted:
        await session.send(RemoteAction.SELECT)

    assert disconnects == [str(interrupted.value)]
    with pytest.raises(pyatv_exceptions.InvalidStateError):
        await session.send(RemoteAction.RIGHT)
    await session.close()
    assert atv.close_calls == 1
    atv.remote_control.right.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_press_is_rejected_without_disconnecting() -> None:
    atv = FakeAppleTV()
    atv.remote_control.play_pause = AsyncMock(
        side_effect=pyatv_exceptions.NotSupportedError("play_pause")
    )
    disconnects: list[str | None] = []
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=disconnects.append,
        on_keyboard_focus=lambda focus: None,
    )

    with pytest.raises(pyatv_exceptions.NotSupportedError):
        await session.send(RemoteAction.PLAY_PAUSE)
    await session.send(RemoteAction.RIGHT)

    assert disconnects == []
    atv.remote_control.right.assert_awaited_once_with(InputAction.SingleTap)
    await session.close()


@pytest.mark.asyncio
async def test_volume_hid_does_not_wait_for_audio_confirmation() -> None:
    atv = FakeAppleTV()
    never_confirmed = asyncio.Event()
    atv.audio.volume_up = AsyncMock(side_effect=never_confirmed.wait)
    atv.audio.volume_down = AsyncMock(side_effect=never_confirmed.wait)
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=lambda error: None,
        on_keyboard_focus=lambda focus: None,
    )

    await asyncio.wait_for(session.send(RemoteAction.VOLUME_UP), timeout=0.1)
    await asyncio.wait_for(session.send(RemoteAction.VOLUME_DOWN), timeout=0.1)
    await session.send(RemoteAction.LEFT)

    atv.remote_control.volume_up.assert_awaited_once_with()
    atv.remote_control.volume_down.assert_awaited_once_with()
    atv.audio.volume_up.assert_not_awaited()
    atv.audio.volume_down.assert_not_awaited()
    atv.remote_control.left.assert_awaited_once_with(InputAction.SingleTap)


@pytest.mark.asyncio
async def test_volume_hid_contains_pyatv_deprecation_warning() -> None:
    atv = FakeAppleTV()
    calls = 0
    deprecated = cast(
        Callable[[Callable[[], Awaitable[None]]], Callable[[], Awaitable[None]]],
        support.deprecated,
    )

    @deprecated
    async def deprecated_volume_up() -> None:
        nonlocal calls
        calls += 1

    atv.remote_control.volume_up = deprecated_volume_up
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=lambda error: None,
        on_keyboard_focus=lambda focus: None,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await session.send(RemoteAction.VOLUME_UP)

    assert calls == 1
    assert caught == []


@pytest.mark.asyncio
async def test_connection_loss_notifies_once_and_cleans_up() -> None:
    atv = FakeAppleTV()
    disconnects: list[str | None] = []
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=disconnects.append,
        on_keyboard_focus=lambda focus: None,
    )

    assert atv.listener is not None
    atv.listener.connection_lost(RuntimeError("network vanished"))
    atv.listener.connection_closed()
    await session.close()

    assert disconnects == ["network vanished"]
    assert atv.close_calls == 1


@pytest.mark.asyncio
async def test_explicit_close_does_not_emit_disconnect() -> None:
    atv = FakeAppleTV()
    disconnects: list[str | None] = []
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=disconnects.append,
        on_keyboard_focus=lambda focus: None,
    )
    listener = atv.listener
    assert listener is not None

    await session.close()
    listener.connection_closed()
    await session.close()

    assert disconnects == []
    assert atv.close_calls == 1


class Sample(NamedTuple):
    """One ``_hidT`` event, its coordinates read the way Apple's OPACK decoder would."""

    x: int
    y: int
    phase: TouchAction
    elapsed: float
    """Virtual seconds requested from ``asyncio.sleep`` before the event was sent."""


def apple_integer(packed: bytes) -> int:
    """Read one packed OPACK integer with Apple's rule: tags 0x30-0x33 are signed.

    pyatv's own ``unpack`` reads them unsigned, so a pyatv round trip cannot
    see the difference (AccessorySDK ``OPACKUtils.c`` ``_OPACKDecodeNumber``).
    """
    tag = packed[0]
    if 0x08 <= tag <= 0x2F:
        assert len(packed) == 1, packed.hex()
        return tag - 0x08
    assert 0x30 <= tag <= 0x33, packed.hex()
    assert len(packed) == 1 + (1 << (tag - 0x30)), packed.hex()
    return int.from_bytes(packed[1:], byteorder="little", signed=True)


class RecordingTouchAPI(CompanionAPI):
    """Record the touch events pyatv would put on the wire, without a transport."""

    def __init__(self, *, block_press: bool = False, fail_hold: int | None = None) -> None:
        super().__init__(cast(Core, object()))
        self.events: list[Sample] = []
        self.misread: list[tuple[int, int]] = []
        """Coordinates whose packed bytes mean something else to Apple: (sent, read)."""
        self.elapsed = 0.0
        self.pressed = asyncio.Event()
        self.release_press = asyncio.Event()
        self.fail_hold = fail_hold
        if not block_press:
            self.release_press.set()

    async def _send_event(self, identifier: str, content: Mapping[str, Any]) -> None:
        assert identifier == "_hidT", identifier
        x, y = (self._coordinate(content[key]) for key in ("_cx", "_cy"))
        sample = Sample(x, y, TouchAction(content["_tPh"]), self.elapsed)
        self.events.append(sample)
        if sample.phase is TouchAction.Press:
            self.pressed.set()
            await self.release_press.wait()
        holds = sum(event.phase is TouchAction.Hold for event in self.events)
        if sample.phase is TouchAction.Hold and holds == self.fail_hold:
            raise pyatv_exceptions.ProtocolError("hold lost")

    def _coordinate(self, sent: int) -> int:
        read = apple_integer(pack(sent))
        if read != sent:
            self.misread.append((int(sent), read))
        return read


def touch_session(
    api: RecordingTouchAPI, *, unavailable: set[FeatureName] | None = None
) -> tuple[FakeAppleTV, PyatvRemoteSession]:
    atv = FakeAppleTV()
    atv.features = AvailableFeatures(unavailable)
    atv.touch = CompanionTouchGestures(api)
    session = PyatvRemoteSession(
        cast(AppleTV, cast(object, atv)),
        on_disconnect=lambda error: None,
        on_keyboard_focus=lambda focus: None,
    )
    return atv, session


def virtual_clock(api: RecordingTouchAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance a virtual clock by each requested sleep instead of waiting for real time."""
    real_sleep = asyncio.sleep

    async def virtual_sleep(delay: float) -> None:
        api.elapsed += delay
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", virtual_sleep)


def contacts(events: list[Sample]) -> list[list[Sample]]:
    """Split a touch stream into press-to-release contacts; fails on stray events."""
    split: list[list[Sample]] = []
    for event in events:
        if event.phase is TouchAction.Press:
            split.append([event])
        else:
            assert split, event
            assert split[-1][-1].phase is not TouchAction.Release, event
            split[-1].append(event)
    assert all(contact[-1].phase is TouchAction.Release for contact in split)
    return split


def assert_monotonic_swipe(contact: list[Sample], axis: int, direction: int) -> None:
    """One contact: pressed at the neutral centre, moved one way, released on the far point."""
    assert contact[0][:3] == (500, 500, TouchAction.Press)
    assert [sample.phase for sample in contact[1:-1]] == [TouchAction.Hold] * (len(contact) - 2)
    assert contact[-1].phase is TouchAction.Release
    positions = [(sample.x, sample.y)[axis] for sample in contact]
    steps = [(later - earlier) * direction for earlier, later in pairwise(positions)]
    assert all(step >= 0 for step in steps), contact
    assert (positions[-1] - positions[0]) * direction > 0
    assert all((position - 500) * direction > 0 for position in positions[1:]), contact
    assert all(0 < sample.x < 1000 and 0 < sample.y < 1000 for sample in contact)
    assert all((sample.x, sample.y)[1 - axis] == 500 for sample in contact)


SWIPES = [
    (RemoteAction.SWIPE_LEFT, 0, -1),
    (RemoteAction.SWIPE_DOWN, 1, 1),
    (RemoteAction.SWIPE_UP, 1, -1),
    (RemoteAction.SWIPE_RIGHT, 0, 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "axis", "direction"), SWIPES)
async def test_swipe_is_one_monotonic_contact_from_the_centre(
    action: RemoteAction, axis: int, direction: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = RecordingTouchAPI()
    atv, session = touch_session(api)
    virtual_clock(api, monkeypatch)

    await session.send(action)
    await session.close()

    # pyatv packs 128..255 with the one-byte OPACK tag Apple reads as a signed
    # int8; a coordinate that arrives negative lies outside the declared
    # surface, which only the decreasing paths used to hit.
    assert api.misread == []
    (contact,) = contacts(api.events)
    assert_monotonic_swipe(contact, axis, direction)
    for button in ("up", "down", "left", "right"):
        getattr(atv.remote_control, button).assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_and_opposite_swipes_only_reset_at_a_new_contact() -> None:
    api = RecordingTouchAPI()
    _, session = touch_session(api)

    await session.send(RemoteAction.SWIPE_LEFT)
    await session.send(RemoteAction.SWIPE_LEFT)
    await session.send(RemoteAction.SWIPE_RIGHT)
    await session.close()

    first, second, third = contacts(api.events)
    assert_monotonic_swipe(first, 0, -1)
    assert_monotonic_swipe(second, 0, -1)
    assert_monotonic_swipe(third, 0, 1)


@pytest.mark.asyncio
async def test_release_is_the_final_paced_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RecordingTouchAPI()
    _, session = touch_session(api)
    virtual_clock(api, monkeypatch)

    await session.send(RemoteAction.SWIPE_LEFT)
    await session.close()

    (contact,) = contacts(api.events)
    assert_monotonic_swipe(contact, 0, -1)
    last_hold, release = contact[-2], contact[-1]
    hold_gaps = [later.elapsed - earlier.elapsed for earlier, later in pairwise(contact[1:-1])]
    # The lift-off is paced like the movement: it lands one hold-to-hold gap
    # after the last hold, not at the same instant.
    assert release.elapsed - last_hold.elapsed == pytest.approx(min(hold_gaps))
    assert min(hold_gaps) > 0
    # The far point is reached only by the lift-off; no stationary hold precedes it.
    assert last_hold.x > release.x


@pytest.mark.asyncio
async def test_failed_hold_releases_where_the_contact_was_left() -> None:
    api = RecordingTouchAPI(fail_hold=3)
    _, session = touch_session(api)

    with pytest.raises(pyatv_exceptions.ProtocolError, match="hold lost") as failure:
        await session.send(RemoteAction.SWIPE_UP)
    await session.close()

    assert failure.value.__context__ is None
    (contact,) = contacts(api.events)
    assert [sample.phase for sample in contact] == [
        TouchAction.Press,
        TouchAction.Hold,
        TouchAction.Hold,
        TouchAction.Hold,
        TouchAction.Release,
    ]
    assert contact[-1][:2] == contact[-2][:2]
    assert_monotonic_swipe(contact, 1, -1)


@pytest.mark.asyncio
async def test_swipe_capability_follows_the_touch_action_feature() -> None:
    api = RecordingTouchAPI()
    _, with_action = touch_session(api, unavailable={FeatureName.Swipe})
    _, without_action = touch_session(api, unavailable={FeatureName.Action})

    assert with_action.capabilities.swipe
    assert without_action.capabilities.navigation
    assert not without_action.capabilities.swipe
    with pytest.raises(pyatv_exceptions.NotSupportedError):
        await without_action.send(RemoteAction.SWIPE_LEFT)
    assert api.events == []
    await with_action.close()
    await without_action.close()


@pytest.mark.asyncio
async def test_close_drains_swipe_after_repeated_cancellation() -> None:
    api = RecordingTouchAPI(block_press=True)
    atv, session = touch_session(api)
    sending = asyncio.create_task(session.send(RemoteAction.SWIPE_RIGHT))
    await api.pressed.wait()
    sending.cancel()
    await asyncio.sleep(0)
    sending.cancel()
    closing = asyncio.create_task(session.close())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not sending.done()
        assert not closing.done()
        assert atv.close_calls == 0
        assert [sample[:3] for sample in api.events] == [(500, 500, TouchAction.Press)]
        with pytest.raises(pyatv_exceptions.InvalidStateError):
            await session.send(RemoteAction.SWIPE_LEFT)
    finally:
        api.release_press.set()
        await asyncio.gather(sending, closing, return_exceptions=True)

    assert sending.cancelled()
    (contact,) = contacts(api.events)
    assert_monotonic_swipe(contact, 0, 1)
    assert atv.close_calls == 1
