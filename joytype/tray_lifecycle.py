"""Close-to-tray lifecycle helpers for tray-resident GUI hosts."""

from __future__ import annotations

from collections.abc import Callable


class CloseToTrayController:
    """Decide whether a window close means hide-to-tray or real shutdown."""

    def __init__(
        self,
        app_name: str,
        on_real_close: Callable[[], None] | None = None,
        notify: Callable[[str, str], None] | None = None,
    ) -> None:
        self._app_name = app_name
        self._on_real_close = on_real_close
        self._notify = notify
        self._real_close_requested = False
        self._real_close_ran = False

    def request_real_close(self) -> None:
        """Make the next close event a real application shutdown."""
        self._real_close_requested = True

    def handle_close(self, event, hide_window: Callable[[], None]) -> bool:
        """Handle a close event.

        Returns ``True`` when the caller should allow shutdown, ``False`` when
        the window was only hidden to the tray.
        """
        if self._real_close_requested:
            if not self._real_close_ran and self._on_real_close is not None:
                self._on_real_close()
                self._real_close_ran = True
            event.accept()
            return True

        event.ignore()
        hide_window()
        return False
