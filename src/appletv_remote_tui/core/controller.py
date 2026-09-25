"""Application orchestration independent of any frontend or transport."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Unpack

from appletv_remote_tui.core.models import (
    AppState,
    ConnectionStatus,
    Device,
    DeviceAvailability,
    KeyboardFocus,
    RemoteAction,
    StateDelta,
)
from appletv_remote_tui.core.ports import (
    AppleTVBackend,
    DeviceHistory,
    PairingSession,
    RemoteSession,
)

_LOGGER = logging.getLogger(__name__)

StateListener = Callable[[AppState], None]
Clock = Callable[[], datetime]
_Status = tuple[ConnectionStatus, str, str | None]

_SCANNING_MESSAGE = "Looking for Apple TVs…"


class ControllerError(RuntimeError):
    """Raised when an action is invalid for the current application state."""


def _order_devices(devices: tuple[Device, ...]) -> tuple[Device, ...]:
    """Order most recently connected first, then by name, then identifier."""

    def order_key(device: Device) -> tuple[int, float, str, str]:
        connected_at = device.last_connected_at
        if connected_at is not None:
            return (0, -connected_at.timestamp(), device.name.casefold(), device.identifier)
        return (1, 0.0, device.name.casefold(), device.identifier)

    return tuple(sorted(devices, key=order_key))


def _find(devices: tuple[Device, ...], identifier: str) -> Device | None:
    return next((device for device in devices if device.identifier == identifier), None)


class RemoteController:
    """Coordinate discovery, pairing, connection, ordered commands, and text editing.

    Text editing follows a small lifecycle: :meth:`begin_text_edit` loads the
    focused tvOS text field into ``state.text_buffer``; :meth:`edit_text`
    records local changes; :meth:`sync_text` mirrors them to the Apple TV;
    :meth:`submit_text` synchronizes and presses Select; :meth:`end_text_edit`
    stops editing. Losing keyboard focus or the connection ends editing.
    """

    def __init__(
        self,
        backend: AppleTVBackend,
        *,
        history: DeviceHistory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.backend = backend
        self._history = history
        self._clock = clock or (lambda: datetime.now(UTC))
        remembered: tuple[Device, ...] = ()
        if history is not None:
            try:
                remembered = tuple(
                    replace(device, availability=DeviceAvailability.REMEMBERED)
                    for device in history.load()
                )
            except Exception:
                _LOGGER.exception("Unable to load remembered Apple TVs")
        self.state = AppState(devices=_order_devices(remembered))
        self._listeners: list[StateListener] = []
        self._session: RemoteSession | None = None
        self._pairing: PairingSession | None = None
        self._command_lock = asyncio.Lock()
        self._text_lock = asyncio.Lock()
        self._keyboard_focus_generation = 0

    def add_listener(self, listener: StateListener) -> None:
        """Subscribe to state changes and immediately receive the current state."""
        self._listeners.append(listener)
        listener(self.state)

    def remove_listener(self, listener: StateListener) -> None:
        """Remove a previously registered listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _update(self, **changes: Unpack[StateDelta]) -> None:
        self.state = replace(self.state, **changes)
        for listener in tuple(self._listeners):
            listener(self.state)

    def _status(self) -> _Status:
        return (self.state.connection, self.state.message, self.state.error)

    def _persist_history(self, devices: tuple[Device, ...]) -> None:
        if self._history is None:
            return
        try:
            self._history.save(
                tuple(device for device in devices if device.last_connected_at is not None)
            )
        except Exception:
            # History is convenience metadata. Never break a live connection
            # because the local history file cannot be updated.
            _LOGGER.exception("Unable to save remembered Apple TVs")

    def _verified_device(self, requested: Device) -> Device:
        device = _find(self.state.devices, requested.identifier)
        if device is None or device.availability not in {
            DeviceAvailability.AVAILABLE,
            DeviceAvailability.CONNECTED,
        }:
            raise ControllerError(f"{requested.name} has not been found in the latest scan")
        return device

    def _is_current_session_device(self, identifier: str) -> bool:
        current = self.state.current_device
        return (
            self._session is not None and current is not None and current.identifier == identifier
        )

    def _devices_after_session_close(self) -> tuple[tuple[Device, ...], Device | None]:
        devices = tuple(
            replace(device, availability=DeviceAvailability.AVAILABLE)
            if device.availability is DeviceAvailability.CONNECTED
            else device
            for device in self.state.devices
        )
        current = self.state.current_device
        if current is not None:
            current = _find(devices, current.identifier) or current
        return devices, current

    async def discover(
        self, hosts: tuple[str, ...] | None = None, scan_timeout: float = 5.0
    ) -> tuple[Device, ...]:
        """Discover Apple TVs and publish a deduplicated, ordered device list.

        Remembered devices within the scan's scope are marked ``CHECKING`` for
        its duration and ``UNAVAILABLE`` if the scan does not find them.
        """
        before_scan = self.state
        full_scan = not hosts
        targeted_addresses = set(hosts or ())
        in_scope = {
            device.identifier
            for device in self.state.devices
            if device.last_connected_at is not None
            and (full_scan or device.address in targeted_addresses)
        }
        checking = tuple(
            replace(device, availability=DeviceAvailability.CHECKING)
            if device.identifier in in_scope
            and device.availability is not DeviceAvailability.CONNECTED
            else device
            for device in self.state.devices
        )
        self._update(
            devices=_order_devices(checking),
            connection=(
                self.state.connection if self._session is not None else ConnectionStatus.DISCOVERING
            ),
            message=_SCANNING_MESSAGE,
            error=None,
        )
        announced = self._status()
        try:
            discovered = await self.backend.discover(hosts, scan_timeout)
        except asyncio.CancelledError:
            previous_availability = {
                device.identifier: device.availability for device in before_scan.devices
            }
            retained = tuple(
                replace(
                    device,
                    availability=previous_availability.get(
                        device.identifier, DeviceAvailability.REMEMBERED
                    ),
                )
                if device.availability is DeviceAvailability.CHECKING
                else device
                for device in self.state.devices
            )
            devices = _order_devices(retained)
            # Only restore the pre-scan status when nothing else has claimed the
            # status line since the scan was announced.
            if self._status() == announced:
                self._update(
                    devices=devices,
                    connection=before_scan.connection,
                    message=before_scan.message,
                    error=before_scan.error,
                )
            else:
                self._update(devices=devices)
            raise
        except Exception as ex:
            retained = tuple(
                replace(device, availability=DeviceAvailability.REMEMBERED)
                if device.availability is DeviceAvailability.CHECKING
                else device
                for device in self.state.devices
            )
            self._update(
                devices=_order_devices(retained),
                connection=(
                    ConnectionStatus.CONNECTED
                    if self._session is not None
                    else ConnectionStatus.ERROR
                ),
                message="Discovery failed",
                error=str(ex),
            )
            raise

        unique = {device.identifier: device for device in discovered}
        previous = {device.identifier: device for device in self.state.devices}
        merged: list[Device] = []
        for identifier, found in unique.items():
            remembered = previous.get(identifier)
            merged.append(
                replace(
                    found,
                    last_connected_at=(
                        remembered.last_connected_at if remembered is not None else None
                    ),
                    availability=(
                        DeviceAvailability.CONNECTED
                        if self._is_current_session_device(identifier)
                        else DeviceAvailability.AVAILABLE
                    ),
                )
            )
        for identifier, remembered in previous.items():
            if identifier in unique or remembered.last_connected_at is None:
                continue
            if self._is_current_session_device(identifier):
                availability = DeviceAvailability.CONNECTED
            elif identifier in in_scope:
                availability = DeviceAvailability.UNAVAILABLE
            else:
                availability = DeviceAvailability.REMEMBERED
            merged.append(replace(remembered, availability=availability))

        ordered = _order_devices(tuple(merged))
        current_device = self.state.current_device
        if current_device is not None:
            current_device = _find(ordered, current_device.identifier) or current_device
        unavailable_count = sum(
            device.availability is DeviceAvailability.UNAVAILABLE for device in ordered
        )
        message = f"Found {len(unique)} Apple TV{'s' if len(unique) != 1 else ''}"
        if unavailable_count:
            message += f" · {unavailable_count} remembered not found"
        self._update(
            connection=(
                ConnectionStatus.CONNECTED if self._session is not None else ConnectionStatus.IDLE
            ),
            devices=ordered,
            current_device=current_device,
            message=message,
        )
        self._persist_history(ordered)
        return ordered

    async def begin_pairing(self, device: Device) -> bool:
        """Begin pairing and return whether the PIN is shown by the device."""
        device = self._verified_device(device)
        await self._close_pairing()
        self._update(
            connection=ConnectionStatus.PAIRING,
            current_device=device,
            message=f"Pairing with {device.name}",
            error=None,
        )
        try:
            self._pairing = await self.backend.begin_pairing(device)
        except Exception as ex:
            self._update(connection=ConnectionStatus.ERROR, message="Pairing failed", error=str(ex))
            raise
        return self._pairing.device_provides_pin

    async def finish_pairing(self, pin: str) -> None:
        """Finish the active pairing attempt."""
        if self._pairing is None or self.state.current_device is None:
            raise ControllerError("No pairing attempt is active")
        try:
            await self._pairing.finish(pin)
            device = replace(self.state.current_device, paired=True)
            devices = tuple(
                device if candidate.identifier == device.identifier else candidate
                for candidate in self.state.devices
            )
            self._update(
                connection=ConnectionStatus.IDLE,
                devices=devices,
                current_device=device,
                message=f"Paired with {device.name}",
                error=None,
            )
        except Exception as ex:
            self._update(connection=ConnectionStatus.ERROR, message="Pairing failed", error=str(ex))
            raise
        finally:
            await self._close_pairing()

    async def connect(self, device: Device) -> None:
        """Connect to a paired device and subscribe to push state."""
        device = self._verified_device(device)
        await self._close_session()
        device = _find(self.state.devices, device.identifier) or device
        self._update(
            connection=ConnectionStatus.CONNECTING,
            current_device=device,
            text_editing=False,
            keyboard_focus=KeyboardFocus.UNKNOWN,
            message=f"Connecting to {device.name}…",
            error=None,
        )
        try:
            session = await self.backend.connect(
                device,
                on_disconnect=self._on_disconnect,
                on_keyboard_focus=self._on_keyboard_focus,
            )
        except Exception as ex:
            self._update(
                connection=ConnectionStatus.ERROR,
                message="Connection failed",
                error=str(ex),
            )
            raise
        self._session = session
        connected_at = self._clock()
        connected_at = (
            connected_at.replace(tzinfo=UTC)
            if connected_at.tzinfo is None
            else connected_at.astimezone(UTC)
        )
        connected_device = replace(
            device,
            last_connected_at=connected_at,
            availability=DeviceAvailability.CONNECTED,
        )
        updated_devices: list[Device] = []
        found = False
        for candidate in self.state.devices:
            if candidate.identifier == connected_device.identifier:
                updated_devices.append(connected_device)
                found = True
            elif candidate.availability is DeviceAvailability.CONNECTED:
                updated_devices.append(
                    replace(candidate, availability=DeviceAvailability.AVAILABLE)
                )
            else:
                updated_devices.append(candidate)
        if not found:
            updated_devices.append(connected_device)
        ordered_devices = _order_devices(tuple(updated_devices))
        self._update(
            connection=ConnectionStatus.CONNECTED,
            devices=ordered_devices,
            current_device=connected_device,
            keyboard_focus=session.keyboard_focus,
            capabilities=session.capabilities,
            message=f"Connected to {device.name}",
            error=None,
        )
        self._persist_history(ordered_devices)

    async def send(self, action: RemoteAction, *, hold: bool = False) -> None:
        """Serialize and send one supported action without replaying it on reconnect.

        ``hold`` keeps a button pressed instead of tapping it; only holdable
        buttons accept it, anything else is refused rather than tapped.
        """
        session = self._require_session()
        if hold and not action.holdable:
            raise ControllerError(f"{action.label} cannot be held")
        if not session.capabilities.supports(action, hold=hold):
            raise ControllerError(f"{action.label} is unavailable")
        outcome, failure = ("held", "hold failed") if hold else ("sent", "failed")
        async with self._command_lock:
            if session is not self._session or not self._connected():
                raise ControllerError("Apple TV disconnected before the command was sent")
            try:
                await session.send(action, hold=hold)
            except Exception as ex:
                if session is self._session and self._connected():
                    self._update(message=f"{action.label} {failure}", error=str(ex))
                raise
            if session is not self._session or not self._connected():
                raise ControllerError("Apple TV disconnected before the command completed")
        self._update(message=f"{action.label} {outcome}", error=None)

    async def begin_text_edit(self) -> str:
        """Load the focused tvOS text field into the local buffer and start editing."""
        session = self._require_session()
        if not session.capabilities.keyboard:
            raise ControllerError("This Apple TV session does not support text input")
        if self.state.keyboard_focus is not KeyboardFocus.FOCUSED:
            raise ControllerError("Open a text field on Apple TV first")
        focus_generation = self._keyboard_focus_generation
        text = await session.get_text()
        if (
            not self._field_usable(session)
            or focus_generation != self._keyboard_focus_generation
            or not session.capabilities.keyboard
        ):
            raise ControllerError("Apple TV text field became unavailable while loading")
        self._update(
            text_editing=True,
            text_buffer=text,
            text_synced=True,
            message="Editing Apple TV text",
            error=None,
        )
        return text

    def end_text_edit(self) -> None:
        """Stop editing without touching the Apple TV; the buffer is retained."""
        self._update(text_editing=False, message="Finished editing")

    def edit_text(self, text: str) -> None:
        """Record a local edit ahead of its (typically debounced) synchronization."""
        if not self.state.text_editing:
            raise ControllerError("No Apple TV text field is open for editing")
        self._update(text_buffer=text, text_synced=False, error=None)

    async def sync_text(self, text: str) -> None:
        """Replace the focused tvOS text field with ``text`` if it is still the buffer."""
        session = self._require_session()
        if not self.state.text_editing:
            raise ControllerError("No Apple TV text field is open for editing")
        if self.state.keyboard_focus is not KeyboardFocus.FOCUSED:
            raise ControllerError("The Apple TV text field is no longer focused")
        if self._buffer_is_synced(text):
            return
        async with self._text_lock:
            if self._buffer_is_synced(text) or text != self.state.text_buffer:
                return
            if not self._field_usable(session) or not self.state.text_editing:
                raise ControllerError("Apple TV text field became unavailable")
            try:
                await session.set_text(text)
            except Exception as ex:
                if session is self._session:
                    self._update(
                        text_buffer=text,
                        text_synced=False,
                        message="Text sync failed",
                        error=str(ex),
                    )
                raise
            if not self._field_usable(session) or not self.state.text_editing:
                raise ControllerError("Apple TV text field became unavailable during sync")
            if text != self.state.text_buffer:
                return
            self._update(text_buffer=text, text_synced=True, message="Text synced", error=None)

    async def submit_text(self, text: str) -> None:
        """Synchronize ``text``, press Select, and stop editing."""
        if text != self.state.text_buffer:
            self.edit_text(text)
        await self.sync_text(text)
        if not self.state.text_synced or self.state.text_buffer != text:
            raise ControllerError("Text changed before it could be submitted")
        await self.send(RemoteAction.SELECT)
        self._update(text_editing=False, message="Text submitted")

    async def close(self) -> None:
        """Close all network resources."""
        await self._close_pairing()
        await self._close_session()
        self._update(connection=ConnectionStatus.DISCONNECTED, message="Disconnected")

    def _connected(self) -> bool:
        return self.state.connection is ConnectionStatus.CONNECTED

    def _require_session(self) -> RemoteSession:
        if self._session is None or not self._connected():
            raise ControllerError("Not connected to an Apple TV")
        return self._session

    def _field_usable(self, session: RemoteSession) -> bool:
        return (
            session is self._session
            and self._connected()
            and self.state.keyboard_focus is KeyboardFocus.FOCUSED
        )

    def _buffer_is_synced(self, text: str) -> bool:
        return self.state.text_synced and text == self.state.text_buffer

    async def _close_pairing(self) -> None:
        if self._pairing is not None:
            pairing, self._pairing = self._pairing, None
            await pairing.close()

    async def _close_session(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            try:
                await session.close()
            finally:
                devices, current_device = self._devices_after_session_close()
                self._update(devices=devices, current_device=current_device)

    def _on_disconnect(self, error: str | None) -> None:
        self._session = None
        self._keyboard_focus_generation += 1
        devices, current_device = self._devices_after_session_close()
        self._update(
            connection=ConnectionStatus.DISCONNECTED,
            devices=devices,
            current_device=current_device,
            text_editing=False,
            text_synced=False,
            message="Apple TV disconnected",
            error=error,
        )

    def _on_keyboard_focus(self, focus: KeyboardFocus) -> None:
        self._keyboard_focus_generation += 1
        if focus is not KeyboardFocus.FOCUSED and self.state.text_editing:
            self._update(
                keyboard_focus=focus,
                text_editing=False,
                text_synced=False,
                message="Apple TV text field lost focus",
            )
        else:
            self._update(keyboard_focus=focus)
