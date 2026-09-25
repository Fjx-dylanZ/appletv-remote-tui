"""Behavioral tests for the toolkit-independent remote controller."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from appletv_remote_tui.core import (
    Capabilities,
    ConnectionStatus,
    ControllerError,
    Device,
    DeviceAvailability,
    KeyboardFocus,
    RemoteAction,
    RemoteController,
)
from tests.fakes import FakeBackend, FakeDeviceHistory, FakeRemoteSession


@pytest.fixture
def device() -> Device:
    return Device(identifier="living-room", name="Living Room", address="192.0.2.10")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def controller(backend: FakeBackend) -> RemoteController:
    return RemoteController(backend)


async def connect(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
    *,
    focus: KeyboardFocus = KeyboardFocus.FOCUSED,
    capabilities: Capabilities | None = None,
    text: str = "",
) -> FakeRemoteSession:
    if not any(candidate.identifier == device.identifier for candidate in controller.state.devices):
        backend.discovered = [device]
        await controller.discover()
    backend.session = FakeRemoteSession(
        capabilities=capabilities,
        keyboard_focus=focus,
        text=text,
    )
    await controller.connect(device)
    return backend.session


async def test_discovery_deduplicates_by_identifier_and_sorts_by_name(
    controller: RemoteController,
    backend: FakeBackend,
) -> None:
    stale_den = Device(identifier="den", name="Zulu", address="192.0.2.1")
    current_den = replace(stale_den, name="den", address="192.0.2.2")
    bedroom = Device(identifier="bedroom", name="Bedroom", address="192.0.2.3")
    backend.discovered = [stale_den, bedroom, current_den]

    devices = await controller.discover(("192.0.2.2",), scan_timeout=1.25)

    assert devices == (bedroom, current_den)
    assert controller.state.devices == devices
    assert controller.state.connection is ConnectionStatus.IDLE
    assert controller.state.error is None
    assert backend.discover_calls == [(("192.0.2.2",), 1.25)]


def test_remembered_devices_load_in_most_recently_connected_order(
    backend: FakeBackend,
) -> None:
    older = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        last_connected_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    recent = Device(
        "bedroom",
        "Bedroom",
        "192.0.2.11",
        last_connected_at=datetime(2026, 7, 18, 13, tzinfo=UTC),
    )

    controller = RemoteController(backend, history=FakeDeviceHistory((older, recent)))

    assert [device.identifier for device in controller.state.devices] == ["bedroom", "living"]
    assert all(
        device.availability is DeviceAvailability.REMEMBERED for device in controller.state.devices
    )


async def test_discovery_refreshes_remembered_address_and_preserves_recency(
    backend: FakeBackend,
) -> None:
    connected_at = datetime(2026, 7, 18, 12, tzinfo=UTC)
    remembered = Device(
        "living",
        "Old name",
        "192.0.2.10",
        model="Old model",
        last_connected_at=connected_at,
    )
    missing = Device(
        "bedroom",
        "Bedroom",
        "192.0.2.11",
        last_connected_at=datetime(2026, 7, 18, 11, tzinfo=UTC),
    )
    history = FakeDeviceHistory((remembered, missing))
    controller = RemoteController(backend, history=history)
    backend.discovered = [
        Device(
            "living",
            "Living Room",
            "192.0.2.99",
            model="Apple TV 4K",
            os_version="18.5",
            paired=True,
        ),
        Device("den", "Den", "192.0.2.12"),
    ]

    devices = await controller.discover()

    assert [device.identifier for device in devices] == ["living", "bedroom", "den"]
    refreshed = devices[0]
    assert refreshed.address == "192.0.2.99"
    assert refreshed.name == "Living Room"
    assert refreshed.model == "Apple TV 4K"
    assert refreshed.last_connected_at == connected_at
    assert refreshed.availability is DeviceAvailability.AVAILABLE
    assert devices[1].availability is DeviceAvailability.UNAVAILABLE
    assert history.saved[-1][0].address == "192.0.2.99"


async def test_scoped_scan_does_not_mark_unrelated_history_unavailable(
    backend: FakeBackend,
) -> None:
    remembered = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        last_connected_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    controller = RemoteController(backend, history=FakeDeviceHistory((remembered,)))
    backend.discovery.enabled = True

    discovery = asyncio.create_task(controller.discover(("192.0.2.200",)))
    await backend.discovery.started.wait()

    assert controller.state.devices[0].availability is DeviceAvailability.REMEMBERED

    backend.discovery.release.set()
    await discovery

    assert controller.state.devices[0].availability is DeviceAvailability.REMEMBERED


async def test_cancelled_discovery_restores_previous_status_and_message(
    backend: FakeBackend,
) -> None:
    remembered = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        last_connected_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    controller = RemoteController(backend, history=FakeDeviceHistory((remembered,)))
    before_scan = controller.state
    backend.discovery.enabled = True

    discovery = asyncio.create_task(controller.discover())
    await backend.discovery.started.wait()
    assert controller.state.devices[0].availability is DeviceAvailability.CHECKING
    assert controller.state.message != before_scan.message

    discovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await discovery

    assert controller.state.devices[0].availability is DeviceAvailability.REMEMBERED
    assert (controller.state.connection, controller.state.message, controller.state.error) == (
        before_scan.connection,
        before_scan.message,
        before_scan.error,
    )


async def test_cancelled_discovery_keeps_status_claimed_while_scanning(
    backend: FakeBackend,
    device: Device,
) -> None:
    controller = RemoteController(backend)
    session = await connect(controller, backend, device)
    backend.discovery.enabled = True

    discovery = asyncio.create_task(controller.discover())
    await backend.discovery.started.wait()
    await controller.send(RemoteAction.UP)
    after_send = controller.state
    discovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await discovery

    assert session.actions == [RemoteAction.UP]
    assert (controller.state.connection, controller.state.message, controller.state.error) == (
        after_send.connection,
        after_send.message,
        after_send.error,
    )


async def test_successful_connect_records_time_and_promotes_device(
    backend: FakeBackend,
) -> None:
    living = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        paired=True,
        last_connected_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    bedroom = Device(
        "bedroom",
        "Bedroom",
        "192.0.2.11",
        paired=True,
        last_connected_at=datetime(2026, 7, 18, 13, tzinfo=UTC),
    )
    connected_at = datetime(2026, 7, 18, 14, tzinfo=UTC)
    history = FakeDeviceHistory((bedroom, living))
    controller = RemoteController(backend, history=history, clock=lambda: connected_at)
    backend.discovered = [living, bedroom]
    await controller.discover()

    await controller.connect(living)

    assert [device.identifier for device in controller.state.devices] == ["living", "bedroom"]
    assert controller.state.devices[0].last_connected_at == connected_at
    assert controller.state.devices[0].availability is DeviceAvailability.CONNECTED
    assert history.saved[-1][0].identifier == "living"
    assert history.saved[-1][0].last_connected_at == connected_at


async def test_failed_connect_does_not_change_recency_or_history(
    backend: FakeBackend,
) -> None:
    connected_at = datetime(2026, 7, 18, 12, tzinfo=UTC)
    living = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        paired=True,
        last_connected_at=connected_at,
    )
    history = FakeDeviceHistory((living,))
    controller = RemoteController(backend, history=history)
    backend.discovered = [living]
    await controller.discover()
    saves_before_connect = len(history.saved)
    backend.connect_error = RuntimeError("unreachable")

    with pytest.raises(RuntimeError, match="unreachable"):
        await controller.connect(living)

    assert controller.state.devices[0].last_connected_at == connected_at
    assert len(history.saved) == saves_before_connect


async def test_failed_device_switch_clears_previous_connected_label(
    backend: FakeBackend,
) -> None:
    living = Device("living", "Living Room", "192.0.2.10", paired=True)
    bedroom = Device("bedroom", "Bedroom", "192.0.2.11", paired=True)
    controller = RemoteController(backend)
    backend.discovered = [living, bedroom]
    await controller.discover()
    await controller.connect(living)
    backend.connect_error = RuntimeError("unreachable")

    with pytest.raises(RuntimeError, match="unreachable"):
        await controller.connect(bedroom)

    living_row = next(
        device for device in controller.state.devices if device.identifier == "living"
    )
    assert living_row.availability is DeviceAvailability.AVAILABLE
    assert all(
        device.availability is not DeviceAvailability.CONNECTED
        for device in controller.state.devices
    )


async def test_unverified_remembered_device_rejects_stale_available_object(
    backend: FakeBackend,
) -> None:
    remembered = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        paired=True,
        last_connected_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    controller = RemoteController(backend, history=FakeDeviceHistory((remembered,)))
    await controller.discover()

    with pytest.raises(ControllerError):
        await controller.connect(remembered)

    assert backend.connect_calls == []


async def test_device_removed_by_rescan_cannot_connect_or_pair_from_stale_selection(
    backend: FakeBackend,
) -> None:
    stale = Device("den", "Den", "192.0.2.12", paired=True)
    controller = RemoteController(backend)
    backend.discovered = [stale]
    await controller.discover()
    backend.discovered = []
    await controller.discover()

    with pytest.raises(ControllerError):
        await controller.connect(stale)
    with pytest.raises(ControllerError):
        await controller.begin_pairing(stale)

    assert backend.connect_calls == []
    assert backend.pairing_calls == []


async def test_discovery_failure_preserves_remembered_devices(
    backend: FakeBackend,
) -> None:
    remembered = Device(
        "living",
        "Living Room",
        "192.0.2.10",
        last_connected_at=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    controller = RemoteController(backend, history=FakeDeviceHistory((remembered,)))
    backend.discover_error = RuntimeError("multicast unavailable")

    with pytest.raises(RuntimeError, match="multicast unavailable"):
        await controller.discover()

    assert len(controller.state.devices) == 1
    assert controller.state.devices[0].identifier == "living"
    assert controller.state.devices[0].availability is DeviceAvailability.REMEMBERED


async def test_history_save_failure_does_not_break_successful_connection(
    backend: FakeBackend,
) -> None:
    history = FakeDeviceHistory()
    history.save_error = OSError("read-only filesystem")
    controller = RemoteController(
        backend,
        history=history,
        clock=lambda: datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    device = Device("living", "Living Room", "192.0.2.10", paired=True)
    backend.discovered = [device]
    await controller.discover()

    await controller.connect(device)

    assert controller.state.connection is ConnectionStatus.CONNECTED
    assert controller.state.current_device is not None
    assert controller.state.current_device.identifier == "living"


async def test_new_discovery_is_not_submitted_to_history_until_connected(
    backend: FakeBackend,
) -> None:
    history = FakeDeviceHistory()
    controller = RemoteController(backend, history=history)
    device = Device("living", "Living Room", "192.0.2.10", paired=True)
    backend.discovered = [device]

    await controller.discover()

    assert history.saved == [()]


async def test_discovery_failure_is_visible_and_reraised(
    controller: RemoteController,
    backend: FakeBackend,
) -> None:
    backend.discover_error = RuntimeError("multicast unavailable")

    with pytest.raises(RuntimeError, match="multicast unavailable"):
        await controller.discover()

    assert controller.state.connection is ConnectionStatus.ERROR
    assert controller.state.error == "multicast unavailable"


async def test_pairing_marks_device_paired_and_always_closes_session(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    backend.discovered = [device]
    await controller.discover()

    provides_pin = await controller.begin_pairing(device)
    await controller.finish_pairing("1234")

    assert provides_pin is True
    assert backend.pairing_calls == [device]
    assert backend.pairing.finish_calls == ["1234"]
    assert backend.pairing.close_calls == 1
    assert controller.state.current_device == replace(device, paired=True)
    assert controller.state.devices == (replace(device, paired=True),)
    assert controller.state.connection is ConnectionStatus.IDLE


async def test_pairing_failure_sets_error_closes_session_and_can_not_be_reused(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    backend.pairing.finish_error = RuntimeError("bad pin")
    backend.discovered = [device]
    await controller.discover()
    await controller.begin_pairing(device)

    with pytest.raises(RuntimeError, match="bad pin"):
        await controller.finish_pairing("0000")

    assert backend.pairing.close_calls == 1
    assert controller.state.connection is ConnectionStatus.ERROR
    assert controller.state.error == "bad pin"
    with pytest.raises(ControllerError):
        await controller.finish_pairing("0000")


async def test_concurrent_actions_are_sent_in_order_without_overlap(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device)
    session.first_send.enabled = True

    first = asyncio.create_task(controller.send(RemoteAction.UP))
    await session.first_send.started.wait()
    second = asyncio.create_task(controller.send(RemoteAction.RIGHT))
    await asyncio.sleep(0)

    assert session.events == ["send-start:up"]
    session.first_send.release.set()
    await asyncio.gather(first, second)

    assert session.actions == [RemoteAction.UP, RemoteAction.RIGHT]
    assert session.events == [
        "send-start:up",
        "send-end:up",
        "send-start:right",
        "send-end:right",
    ]


async def test_unsupported_action_is_refused_before_reaching_the_session(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(
        controller, backend, device, capabilities=Capabilities(swipe=False, volume=False)
    )

    with pytest.raises(ControllerError):
        await controller.send(RemoteAction.SWIPE_LEFT)
    with pytest.raises(ControllerError):
        await controller.send(RemoteAction.VOLUME_UP)
    await controller.send(RemoteAction.LEFT)

    assert session.actions == [RemoteAction.LEFT]
    assert controller.state.error is None


@pytest.mark.parametrize(
    ("action", "flag"),
    [
        (RemoteAction.SELECT, "navigation"),
        (RemoteAction.SWIPE_UP, "swipe"),
        (RemoteAction.MENU, "menu"),
        (RemoteAction.HOME, "home"),
        (RemoteAction.PLAY_PAUSE, "play_pause"),
        (RemoteAction.VOLUME_DOWN, "volume"),
        (RemoteAction.POWER_OFF, "power"),
    ],
)
def test_each_action_is_gated_by_exactly_one_capability(action: RemoteAction, flag: str) -> None:
    nothing = Capabilities(navigation=False, menu=False)
    only_flag = replace(nothing, **{flag: True})

    assert not nothing.supports(action)
    assert only_flag.supports(action)


@pytest.mark.parametrize(
    "action",
    [
        RemoteAction.SWIPE_LEFT,
        RemoteAction.PLAY_PAUSE,
        RemoteAction.VOLUME_UP,
        RemoteAction.POWER_ON,
    ],
)
async def test_hold_of_unholdable_action_is_refused_without_tapping(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
    action: RemoteAction,
) -> None:
    session = await connect(controller, backend, device)

    assert not session.capabilities.supports(action, hold=True)
    with pytest.raises(ControllerError):
        await controller.send(action, hold=True)

    assert session.actions == []
    assert session.holds == []


async def test_hold_requires_the_button_capability(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device, capabilities=Capabilities(home=False))

    with pytest.raises(ControllerError):
        await controller.send(RemoteAction.HOME, hold=True)
    await controller.send(RemoteAction.SELECT, hold=True)

    assert session.holds == [RemoteAction.SELECT]


async def test_failed_hold_propagates_the_transport_error(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device)
    session.send_error = RuntimeError("hid down lost")

    with pytest.raises(RuntimeError, match="hid down lost"):
        await controller.send(RemoteAction.MENU, hold=True)

    assert session.holds == []
    assert controller.state.error == "hid down lost"


async def test_text_edit_loads_remote_text_and_ending_only_changes_mode(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device, text="old query")

    text = await controller.begin_text_edit()
    controller.edit_text("new query")
    controller.end_text_edit()

    assert text == "old query"
    assert controller.state.text_editing is False
    assert controller.state.text_buffer == "new query"
    assert controller.state.text_synced is False
    assert session.actions == []


@pytest.mark.parametrize(
    ("focus", "capabilities"),
    [
        (KeyboardFocus.UNFOCUSED, Capabilities(keyboard=True)),
        (KeyboardFocus.FOCUSED, Capabilities(keyboard=False)),
    ],
)
async def test_text_edit_requires_keyboard_capability_and_focus(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
    focus: KeyboardFocus,
    capabilities: Capabilities,
) -> None:
    session = await connect(controller, backend, device, focus=focus, capabilities=capabilities)

    with pytest.raises(ControllerError):
        await controller.begin_text_edit()

    assert "get-text" not in session.events
    assert controller.state.text_editing is False


@pytest.mark.parametrize("loss", ["focus", "disconnect"])
async def test_text_edit_rejects_text_loaded_after_field_becomes_unavailable(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
    loss: str,
) -> None:
    session = await connect(controller, backend, device, text="stale")
    session.get_text_gate.enabled = True

    loading = asyncio.create_task(controller.begin_text_edit())
    await session.get_text_gate.started.wait()
    if loss == "focus":
        backend.set_keyboard_focus(KeyboardFocus.UNFOCUSED)
    else:
        backend.disconnect("connection reset")
    session.get_text_gate.release.set()

    with pytest.raises(ControllerError):
        await loading
    assert controller.state.text_editing is False


async def test_text_edit_rejects_stale_text_after_focus_moves_away_and_back(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device, text="old field")
    session.get_text_gate.enabled = True

    loading = asyncio.create_task(controller.begin_text_edit())
    await session.get_text_gate.started.wait()
    backend.set_keyboard_focus(KeyboardFocus.UNFOCUSED)
    backend.set_keyboard_focus(KeyboardFocus.FOCUSED)
    session.get_text_gate.release.set()

    with pytest.raises(ControllerError):
        await loading
    assert controller.state.keyboard_focus is KeyboardFocus.FOCUSED
    assert controller.state.text_editing is False


def test_local_text_can_only_be_edited_while_a_field_is_open(
    controller: RemoteController,
) -> None:
    with pytest.raises(ControllerError):
        controller.edit_text("query")


async def test_submit_synchronizes_text_before_selecting(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device, text="")
    await controller.begin_text_edit()

    await controller.submit_text("café")

    assert session.text == "café"
    assert session.actions == [RemoteAction.SELECT]
    assert session.events == [
        "get-text",
        "set-text-start:café",
        "set-text-end:café",
        "send-start:select",
        "send-end:select",
    ]
    assert controller.state.text_editing is False
    assert controller.state.text_synced is True


async def test_submit_does_not_resynchronize_an_already_synced_buffer(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device, text="query")
    await controller.begin_text_edit()

    await controller.submit_text("query")

    assert session.actions == [RemoteAction.SELECT]
    assert session.events == ["get-text", "send-start:select", "send-end:select"]


async def test_failed_text_sync_prevents_select_and_keeps_editing(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device)
    await controller.begin_text_edit()
    session.set_text_error = RuntimeError("keyboard went away")

    with pytest.raises(RuntimeError, match="keyboard went away"):
        await controller.submit_text("keep me")

    assert session.actions == []
    assert controller.state.text_editing is True
    assert controller.state.text_buffer == "keep me"
    assert controller.state.text_synced is False
    assert controller.state.error == "keyboard went away"


async def test_focus_loss_ends_editing_and_marks_buffer_unsynced(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    await connect(controller, backend, device)
    await controller.begin_text_edit()
    controller.edit_text("unfinished")

    backend.set_keyboard_focus(KeyboardFocus.UNFOCUSED)

    assert controller.state.keyboard_focus is KeyboardFocus.UNFOCUSED
    assert controller.state.text_editing is False
    assert controller.state.text_buffer == "unfinished"
    assert controller.state.text_synced is False


async def test_focus_loss_during_sync_prevents_select_and_stale_synced_state(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device)
    await controller.begin_text_edit()
    session.set_text_gate.enabled = True

    submission = asyncio.create_task(controller.submit_text("unfinished"))
    await session.set_text_gate.started.wait()
    backend.set_keyboard_focus(KeyboardFocus.UNFOCUSED)
    session.set_text_gate.release.set()

    with pytest.raises(ControllerError):
        await submission
    assert session.actions == []
    assert controller.state.text_editing is False
    assert controller.state.keyboard_focus is KeyboardFocus.UNFOCUSED
    assert controller.state.text_buffer == "unfinished"
    assert controller.state.text_synced is False


async def test_disconnect_ends_editing_and_rejects_future_commands(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device)
    await controller.begin_text_edit()

    backend.disconnect("connection reset")

    assert controller.state.connection is ConnectionStatus.DISCONNECTED
    assert controller.state.text_editing is False
    assert controller.state.text_synced is False
    assert controller.state.error == "connection reset"
    assert controller.state.devices[0].availability is DeviceAvailability.AVAILABLE
    with pytest.raises(ControllerError):
        await controller.send(RemoteAction.LEFT)
    assert session.actions == []


async def test_disconnect_drops_queued_command_and_preserves_disconnect_message(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    session = await connect(controller, backend, device)
    session.first_send.enabled = True

    in_flight = asyncio.create_task(controller.send(RemoteAction.UP))
    await session.first_send.started.wait()
    queued = asyncio.create_task(controller.send(RemoteAction.RIGHT))
    await asyncio.sleep(0)

    backend.disconnect("connection reset")
    after_disconnect = controller.state
    session.first_send.release.set()
    results = await asyncio.gather(in_flight, queued, return_exceptions=True)

    assert all(isinstance(result, ControllerError) for result in results)
    assert session.actions == [RemoteAction.UP]
    assert "send-start:right" not in session.events
    assert controller.state.connection is ConnectionStatus.DISCONNECTED
    assert (controller.state.message, controller.state.error) == (
        after_disconnect.message,
        after_disconnect.error,
    )


async def test_send_error_after_disconnect_preserves_disconnect_state(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    class DisconnectingSession(FakeRemoteSession):
        async def send(self, action: RemoteAction, *, hold: bool = False) -> None:
            del action, hold
            backend.disconnect("connection reset")
            raise RuntimeError("socket closed")

    backend.discovered = [device]
    await controller.discover()
    backend.session = DisconnectingSession()
    await controller.connect(device)

    with pytest.raises(RuntimeError, match="socket closed"):
        await controller.send(RemoteAction.VOLUME_UP)

    assert controller.state.connection is ConnectionStatus.DISCONNECTED
    assert controller.state.error == "connection reset"


async def test_close_releases_pairing_and_remote_session_and_is_idempotent(
    controller: RemoteController,
    backend: FakeBackend,
    device: Device,
) -> None:
    backend.discovered = [device]
    await controller.discover()
    await controller.begin_pairing(device)
    session = await connect(controller, backend, device)

    await controller.close()
    await controller.close()

    assert backend.pairing.close_calls == 1
    assert session.close_calls == 1
    assert controller.state.connection is ConnectionStatus.DISCONNECTED
