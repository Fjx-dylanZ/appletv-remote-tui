"""Base class for screens that render controller state."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.screen import Screen

from appletv_remote_tui.core import AppState, RemoteController

if TYPE_CHECKING:
    from appletv_remote_tui.tui.app import RemoteTuiApp


class ControllerScreen(Screen[None]):
    """A full screen driven by :class:`RemoteController` state."""

    def __init__(self, controller: RemoteController) -> None:
        super().__init__()
        self.controller = controller

    @property
    def remote_app(self) -> RemoteTuiApp:
        """Return the concrete application hosting this screen."""
        return cast("RemoteTuiApp", self.app)

    def update_state(self, state: AppState) -> None:
        """Render a newly published controller state."""
        raise NotImplementedError

    async def action_quit_app(self) -> None:
        """Close transport resources before leaving the terminal."""
        await self.remote_app.shutdown()
