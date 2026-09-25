"""Companion-only pyatv implementation of the application backend ports."""

from __future__ import annotations

import asyncio
import logging
import math
import warnings
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import pyatv
from pyatv import exceptions as pyatv_exceptions
from pyatv.const import (
    DeviceModel,
    FeatureName,
    FeatureState,
    InputAction,
    KeyboardFocusState,
    Protocol,
    TouchAction,
)
from pyatv.interface import (
    AppleTV,
    BaseConfig,
    DeviceListener,
    KeyboardListener,
    PairingHandler,
)
from pyatv.support.state_producer import StateProducer

from appletv_remote_tui.apple_tv.storage import SecureFileStorage, create_storage
from appletv_remote_tui.core.models import Capabilities, Device, KeyboardFocus, RemoteAction

_LOGGER = logging.getLogger(__name__)

# One swipe is a single touch contact pressed at the neutral centre of the
# 1000x1000 Companion touchpad and slid 300 units towards the requested edge in
# 16 paced samples spread over the requested 250 ms (about the ~16 ms cadence
# the Remote app is observed to use); the last sample is the release itself.
# Stopping at 200/800 keeps clear of the edges, where tvOS reverses the cursor
# unless the contact is released and re-pressed.
_SWIPE_DURATION_MS = 250
_SWIPE_STEPS = 16
_SWIPE_STEP_S = _SWIPE_DURATION_MS / _SWIPE_STEPS / 1000
_SWIPE_DISTANCE = 300
_TOUCH_CENTER = 500
_SWIPE_VECTORS = {
    RemoteAction.SWIPE_LEFT: (-1, 0),
    RemoteAction.SWIPE_RIGHT: (1, 0),
    RemoteAction.SWIPE_UP: (0, -1),
    RemoteAction.SWIPE_DOWN: (0, 1),
}


class _Coordinate(int):
    """Touchpad coordinate that pyatv packs with the two-byte OPACK integer tag.

    Workaround for pyatv 0.18: ``pyatv.support.opack.pack`` picks the OPACK
    integer width from magnitude alone and gives 40..255 the one-byte tag
    (0x30). Apple's OPACK decoder reads that tag as a signed int8
    (AccessorySDK ``OPACKUtils.c``, ``_OPACKDecodeNumber``), and its encoder
    only emits it for -128..127, so 128..255 arrive on the device as negative
    coordinates; the CoreUtils ``OPACKDecodeBytes`` on macOS reads pyatv's
    ``30 c8`` as -56. ``size`` is the width hint pyatv's packer honours for
    ints it decoded itself; declaring it here keeps every coordinate on the
    wire as the int16 Apple's own encoder would produce for 128..32767.
    """

    size = 2


class DeviceNotDiscoveredError(LookupError):
    """Raised when an operation targets a device not returned by discovery."""


def _keyboard_focus(state: KeyboardFocusState) -> KeyboardFocus:
    return {
        KeyboardFocusState.Focused: KeyboardFocus.FOCUSED,
        KeyboardFocusState.Unfocused: KeyboardFocus.UNFOCUSED,
        KeyboardFocusState.Unknown: KeyboardFocus.UNKNOWN,
    }[state]


def _is_available(atv: AppleTV, *features: FeatureName) -> bool:
    return atv.features.in_state(FeatureState.Available, *features)


def _capabilities(atv: AppleTV) -> Capabilities:
    return Capabilities(
        navigation=_is_available(
            atv,
            FeatureName.Up,
            FeatureName.Down,
            FeatureName.Left,
            FeatureName.Right,
            FeatureName.Select,
        ),
        menu=_is_available(atv, FeatureName.Menu),
        home=_is_available(atv, FeatureName.Home),
        play_pause=_is_available(atv, FeatureName.PlayPause),
        volume=_is_available(atv, FeatureName.VolumeUp, FeatureName.VolumeDown),
        power=_is_available(atv, FeatureName.TurnOn, FeatureName.TurnOff),
        keyboard=_is_available(
            atv,
            FeatureName.TextFocusState,
            FeatureName.TextGet,
            FeatureName.TextSet,
        ),
        swipe=_is_available(atv, FeatureName.Action),
    )


class _DeviceEvents(DeviceListener):
    def __init__(self, disconnected: Callable[[str | None], None]) -> None:
        self._disconnected = disconnected

    def connection_lost(self, exception: Exception) -> None:
        self._disconnected(str(exception) or exception.__class__.__name__)

    def connection_closed(self) -> None:
        self._disconnected(None)


class _KeyboardEvents(KeyboardListener):
    def __init__(self, updated: Callable[[KeyboardFocus], None]) -> None:
        self._updated = updated

    def focusstate_update(
        self,
        old_state: KeyboardFocusState,
        new_state: KeyboardFocusState,
    ) -> None:
        del old_state
        self._updated(_keyboard_focus(new_state))


class PyatvRemoteSession:
    """Connected Companion session implementing ``RemoteSession``."""

    def __init__(
        self,
        atv: AppleTV,
        *,
        on_disconnect: Callable[[str | None], None],
        on_keyboard_focus: Callable[[KeyboardFocus], None],
    ) -> None:
        self._atv = atv
        self._on_disconnect = on_disconnect
        self._on_keyboard_focus = on_keyboard_focus
        self._capabilities = _capabilities(atv)
        self._closing = False
        self._closed = False
        self._disconnect_notified = False
        self._close_task: asyncio.Task[None] | None = None
        self._release_safe_tasks: set[asyncio.Task[None]] = set()
        self._device_events = _DeviceEvents(self._disconnected)
        self._keyboard_events = _KeyboardEvents(self._focus_changed)

        atv.listener = self._device_events
        if self._capabilities.keyboard:
            atv.keyboard.listener = self._keyboard_events
            self._keyboard_focus = _keyboard_focus(atv.keyboard.text_focus_state)
        else:
            self._keyboard_focus = KeyboardFocus.UNKNOWN

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def keyboard_focus(self) -> KeyboardFocus:
        return self._keyboard_focus

    async def send(self, action: RemoteAction, *, hold: bool = False) -> None:
        """Send one action through the appropriate Companion interface.

        Every accepted control runs through ``_run_release_safe``: it is drained
        by ``close`` before the transport goes away, and a normal down/up (or
        touch press/release) completes even when the caller is cancelled. A
        control that fails part-way leaves the physical state unknown; see that
        helper for how the session then fails closed.
        """
        self._ensure_open()
        if hold:
            # Companion keeps the HID button down for one second.
            await self._run_release_safe(self._button(action)(InputAction.Hold))
            return
        command: Callable[[], Coroutine[Any, Any, None]]
        uses_deprecated_hid_volume = False
        match action:
            case (
                RemoteAction.UP
                | RemoteAction.DOWN
                | RemoteAction.LEFT
                | RemoteAction.RIGHT
                | RemoteAction.SELECT
                | RemoteAction.MENU
                | RemoteAction.HOME
            ):
                await self._run_release_safe(self._button(action)(InputAction.SingleTap))
                return
            case (
                RemoteAction.SWIPE_LEFT
                | RemoteAction.SWIPE_DOWN
                | RemoteAction.SWIPE_UP
                | RemoteAction.SWIPE_RIGHT
            ):
                if not self._capabilities.swipe:
                    raise pyatv_exceptions.NotSupportedError("swipe is not supported")
                await self._run_release_safe(self._swipe(action))
                return
            case RemoteAction.PLAY_PAUSE:
                command = self._atv.remote_control.play_pause
            case RemoteAction.VOLUME_UP:
                command = self._atv.remote_control.volume_up
                uses_deprecated_hid_volume = True
            case RemoteAction.VOLUME_DOWN:
                command = self._atv.remote_control.volume_down
                uses_deprecated_hid_volume = True
            case RemoteAction.POWER_ON:
                command = self._atv.power.turn_on
            case RemoteAction.POWER_OFF:
                command = self._atv.power.turn_off
        if uses_deprecated_hid_volume:
            # Companion Audio waits for a volume-state event after sending the
            # same HID press. HDMI-CEC/receiver setups often apply the command
            # without publishing that event, causing a false five-second
            # TimeoutError. The RemoteControl API returns after HID down/up.
            # Its facade method is deprecated, so suppress only the warning
            # emitted while creating this coroutine.
            with warnings.catch_warnings(record=True):
                operation = command()
        else:
            operation = command()
        await self._run_release_safe(operation)

    def _button(self, action: RemoteAction) -> Callable[[InputAction], Coroutine[Any, Any, None]]:
        """Return the Companion HID button that accepts an ``InputAction`` for ``action``."""
        remote = self._atv.remote_control
        match action:
            case RemoteAction.UP:
                return remote.up
            case RemoteAction.DOWN:
                return remote.down
            case RemoteAction.LEFT:
                return remote.left
            case RemoteAction.RIGHT:
                return remote.right
            case RemoteAction.SELECT:
                return remote.select
            case RemoteAction.MENU:
                return remote.menu
            case RemoteAction.HOME:
                return remote.home
            case _:
                raise pyatv_exceptions.NotSupportedError(f"{action.label} cannot be held")

    async def _swipe(self, action: RemoteAction) -> None:
        """Slide one contact from the touchpad centre towards the requested edge.

        pyatv's ``touch.swipe`` interpolates against the wall clock; once the
        remaining time drops below one step it overshoots the end point (to the
        edge on a busy event loop) and then releases back onto it, so the
        gesture ends with movement against the requested direction. Emitting
        the contact explicitly keeps every event monotonic along one axis.

        The release is the final paced sample: it lands on the end point one
        step after the last hold, the way a captured Remote-app contact ends
        with a moving lift-off rather than a hold followed by a stationary
        release at the same point and instant. A contact that failed part-way
        is released where it was left, never advanced, so the next gesture
        starts from a fresh press.
        """
        dx, dy = _SWIPE_VECTORS[action]
        touch = self._atv.touch
        x = y = _Coordinate(_TOUCH_CENTER)
        try:
            await touch.action(x, y, TouchAction.Press)
            for step in range(1, _SWIPE_STEPS + 1):
                await asyncio.sleep(_SWIPE_STEP_S)
                offset = _SWIPE_DISTANCE * step // _SWIPE_STEPS
                x = _Coordinate(_TOUCH_CENTER + dx * offset)
                y = _Coordinate(_TOUCH_CENTER + dy * offset)
                if step < _SWIPE_STEPS:
                    await touch.action(x, y, TouchAction.Hold)
        finally:
            await touch.action(x, y, TouchAction.Release)

    async def _run_release_safe(self, operation: Coroutine[Any, Any, None]) -> None:
        """Run one HID operation to its release before propagating cancellation.

        The task is also what ``close`` drains, so a press already on the wire
        gets its release before the transport goes away. Nothing here adds a
        deadline: pyatv bounds each ``_hidC`` exchange with its own five-second
        response timeout and touch events are plain writes.

        A press that ends without its normal completion once it may have
        reached the wire (a lost response, a dropped transport, the operation
        itself cancelled) leaves the device's button state unknown, and pyatv
        offers no release-only call to repair it; normal completion only means
        the release sequence was handed to the transport (touch events carry
        no acknowledgement), never that the device applied it. So the session stops:
        it reports a disconnect once and closes, nothing else is sent until
        the consumer reconnects explicitly, and no repair is attempted. That
        happens even when the caller was cancelled meanwhile, so cancellation
        never hides the failure. A pre-wire rejection (``NotSupportedError``)
        changes nothing.
        """
        task: asyncio.Task[None] = asyncio.create_task(operation)
        self._release_safe_tasks.add(task)
        task.add_done_callback(self._release_safe_tasks.discard)
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
        failure: BaseException | None
        if task.cancelled():
            failure = pyatv_exceptions.CommandError("Apple TV command was interrupted")
        else:
            failure = task.exception()
        if failure is not None and not isinstance(failure, pyatv_exceptions.NotSupportedError):
            self._disconnected(str(failure))
        if cancelled:
            if failure is not None:
                _LOGGER.debug("Gesture failed while completing its release", exc_info=failure)
            raise asyncio.CancelledError
        if failure is not None:
            raise failure

    async def get_text(self) -> str:
        self._ensure_open()
        if not self._capabilities.keyboard:
            raise pyatv_exceptions.NotSupportedError("text input is not supported")
        return await self._atv.keyboard.text_get() or ""

    async def set_text(self, text: str) -> None:
        self._ensure_open()
        if not self._capabilities.keyboard:
            raise pyatv_exceptions.NotSupportedError("text input is not supported")
        await self._atv.keyboard.text_set(text)

    async def close(self) -> None:
        """Close pyatv and wait for all protocol cleanup tasks."""
        await asyncio.shield(self._begin_close())

    def _begin_close(self) -> asyncio.Task[None]:
        """Refuse new commands from this point; in-flight ones still finish."""
        self._closing = not self._closed
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_transport())
        return self._close_task

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise pyatv_exceptions.InvalidStateError("Apple TV session is closed")

    def _focus_changed(self, focus: KeyboardFocus) -> None:
        if self._closing or self._closed:
            return
        self._keyboard_focus = focus
        try:
            self._on_keyboard_focus(focus)
        except Exception:
            _LOGGER.exception("Keyboard focus callback failed")

    def _disconnected(self, error: str | None) -> None:
        if self._closing or self._closed or self._disconnect_notified:
            return
        self._disconnect_notified = True
        try:
            self._on_disconnect(error)
        except Exception:
            _LOGGER.exception("Disconnect callback failed")
        self._begin_close()

    async def _close_transport(self) -> None:
        if self._closed:
            return
        if self._release_safe_tasks:
            results = await asyncio.gather(
                *tuple(self._release_safe_tasks),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    _LOGGER.debug("Gesture cleanup failed", exc_info=result)
        # pyatv documents None for detaching listeners, but its setter is non-optional.
        cast(StateProducer[DeviceListener | None], self._atv).listener = None
        if self._capabilities.keyboard:
            cast(StateProducer[KeyboardListener | None], self._atv.keyboard).listener = None
        try:
            pending = cast(Callable[[], set[asyncio.Task[None]]], self._atv.close)()
            if pending:
                results = await asyncio.gather(*pending, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        _LOGGER.debug("pyatv cleanup failed", exc_info=result)
        finally:
            self._closed = True
            self._closing = False


class PyatvPairingSession:
    """In-progress Companion PIN pairing session."""

    def __init__(
        self,
        handler: PairingHandler,
        config: BaseConfig,
        storage: SecureFileStorage,
    ) -> None:
        self._handler = handler
        self._config = config
        self._storage = storage
        self._closed = False
        self._finished = False

    @property
    def device_provides_pin(self) -> bool:
        return self._handler.device_provides_pin

    async def finish(self, pin: str) -> None:
        if self._closed:
            raise pyatv_exceptions.InvalidStateError("Pairing session is closed")
        if self._finished:
            raise pyatv_exceptions.InvalidStateError("Pairing has already finished")
        normalized_pin = pin.strip()
        if not normalized_pin or not normalized_pin.isascii() or not normalized_pin.isdigit():
            raise ValueError("PIN must contain ASCII digits only")

        cast(Callable[[str], None], self._handler.pin)(normalized_pin)
        await self._handler.finish()
        if not self._handler.has_paired:
            raise pyatv_exceptions.PairingError("Apple TV rejected the pairing request")

        self._finished = True
        await self._storage.update_settings(self._config)
        await self._storage.save()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._handler.close()


class PyatvBackend:
    """Discover, pair, and connect to Apple TVs over Companion."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path = storage_path
        self._storage: SecureFileStorage | None = None
        self._storage_lock = asyncio.Lock()
        self._configs: dict[str, BaseConfig] = {}

    async def discover(
        self,
        hosts: tuple[str, ...] | None = None,
        scan_timeout: float = 5.0,
    ) -> list[Device]:
        if not math.isfinite(scan_timeout) or scan_timeout <= 0:
            raise ValueError("scan_timeout must be a positive finite number")
        storage = await self._get_storage()
        configurations = await pyatv.scan(
            asyncio.get_running_loop(),
            timeout=max(1, math.ceil(scan_timeout)),
            protocol=Protocol.Companion,
            hosts=list(hosts) if hosts else None,
            storage=storage,
        )

        devices: dict[str, Device] = {}
        for config in configurations:
            service = config.get_service(Protocol.Companion)
            if service is None or service.identifier is None:
                continue
            identifier = service.identifier
            self._configs[identifier] = config
            model = (
                None
                if config.device_info.model is DeviceModel.Unknown
                and config.device_info.raw_model is None
                else config.device_info.model_str
            )
            devices[identifier] = Device(
                identifier=identifier,
                name=config.name,
                address=str(config.address),
                model=model,
                os_version=config.device_info.version,
                deep_sleep=config.deep_sleep,
                paired=bool(service.credentials),
            )
        return list(devices.values())

    async def begin_pairing(self, device: Device) -> PyatvPairingSession:
        config = self._config_for(device)
        storage = await self._get_storage()
        pair = cast(Callable[..., Awaitable[PairingHandler]], pyatv.pair)
        handler = await pair(
            config,
            Protocol.Companion,
            asyncio.get_running_loop(),
            storage=storage,
            name="appletv-remote-tui",
        )
        try:
            await handler.begin()
        except BaseException:
            await handler.close()
            raise
        return PyatvPairingSession(handler, config, storage)

    async def connect(
        self,
        device: Device,
        *,
        on_disconnect: Callable[[str | None], None],
        on_keyboard_focus: Callable[[KeyboardFocus], None],
    ) -> PyatvRemoteSession:
        config = self._config_for(device)
        storage = await self._get_storage()
        atv = await pyatv.connect(
            config,
            asyncio.get_running_loop(),
            protocol=Protocol.Companion,
            storage=storage,
        )
        try:
            return PyatvRemoteSession(
                atv,
                on_disconnect=on_disconnect,
                on_keyboard_focus=on_keyboard_focus,
            )
        except BaseException:
            pending = cast(Callable[[], set[asyncio.Task[None]]], atv.close)()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise

    async def forget(self, device: Device) -> None:
        storage = await self._get_storage()
        matching_settings = [
            settings
            for settings in storage.settings
            if settings.protocols.companion.identifier == device.identifier
        ]
        for settings in matching_settings:
            await storage.remove_settings(settings)
        await storage.save()

        config = self._configs.get(device.identifier)
        if config is not None:
            service = config.get_service(Protocol.Companion)
            if service is not None:
                service.credentials = None

    async def _get_storage(self) -> SecureFileStorage:
        if self._storage is None:
            async with self._storage_lock:
                if self._storage is None:
                    self._storage = await create_storage(self.storage_path)
        return self._storage

    def _config_for(self, device: Device) -> BaseConfig:
        config = self._configs.get(device.identifier)
        if config is None:
            raise DeviceNotDiscoveredError(
                f"{device.name} must be discovered before pairing or connecting"
            )
        return config
