"""igni's windows, as the host can see them today.

A window is a view of igni — a desktop window, an IDE panel, a browser
tab. Each has a stable id that survives reloads: the ``client_id`` its
UI state is stored under (``clientState.ts`` in the web client). The
host learns of a window when it reads that state on connect, and that
it went away when its connection closes.

**What is in the model now, and what is not.** The state a window
round-trips through ``client_state`` — the session it is bound to, its
page stack, its sidebar, its drafts — is the host's to read and, by
writing it and telling the window (the ``client_state_changed`` push),
to change. Everything else a window shows still lives inside the web
client's components, which is why ``host.ui.panel`` and its siblings
are stage 2 in the schema.

**The active window** is the one the person last did something in —
wrote its state, typed, or connected. ``host.ui`` addresses it. Which
window has *focus* is not something a window reports yet
(``host.windows.focused`` is stage 2); the active window is the honest
stand-in, and the two agree whenever there is one window.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class UiKeys:
    """The ``client_state`` keys a window round-trips. ``App.tsx`` names
    them; a change to one there is a change here."""

    SESSION = "session-id"
    PROJECT_DIR = "project-dir"
    PAGE = "page"
    SIDEBAR = "sidebar-open"
    DRAFT_PREFIX = "draft:"

    @classmethod
    def draft(cls, session_id: str) -> str:
        return f"{cls.DRAFT_PREFIX}{session_id}"


class WindowRecord(BaseModel):
    id: str
    kind: str = "web"
    connection_id: str = ""
    connected: bool = True

    def describe(self, state: dict[str, str]) -> dict[str, Any]:
        """As ``host.windows`` reports it (the schema's ``Window``)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "connected": self.connected,
            "focused": False,
            "session_id": state.get(UiKeys.SESSION) or None,
            "project_dir": state.get(UiKeys.PROJECT_DIR) or None,
        }


#: Told a window's state changed: (window id, key, value or "" for deleted).
StateListener = Callable[[str, str, str], None]
WindowListener = Callable[[str, dict[str, Any]], None]


class Windows:
    """Every window this backend has seen, and their state."""

    def __init__(
        self,
        store: Any,
        *,
        push: Callable[[str, str, str], None],
    ) -> None:
        self._store = store
        self._push = push
        self._windows: dict[str, WindowRecord] = {}
        self._active: str | None = None
        self._tick = itertools.count()
        self._last_used: dict[str, int] = {}
        #: What each window's state was last seen as, so a write of the
        #: same value — a window re-saving its session on every reload —
        #: is not announced as a change.
        self._seen: dict[tuple[str, str], str] = {}
        self._state_listeners: list[StateListener] = []
        self._window_listeners: list[WindowListener] = []

    # ── Listeners ─────────────────────────────────────────────────

    def on_state(self, listener: StateListener) -> None:
        self._state_listeners.append(listener)

    def on_window(self, listener: WindowListener) -> None:
        """``listener("opened" | "closed", window)``."""
        self._window_listeners.append(listener)

    # ── What the host observes ────────────────────────────────────

    def attached(self, window_id: str, *, connection_id: str = "", kind: str = "") -> None:
        """A window read its state on connect: it is open."""
        if not window_id:
            return
        record = self._windows.get(window_id)
        opened = record is None or not record.connected
        if record is None:
            record = WindowRecord(id=window_id)
            self._windows[window_id] = record
        record.connected = True
        for key, value in self.state(window_id).items():
            self._seen[(window_id, key)] = value
        if connection_id:
            record.connection_id = connection_id
        if kind:
            record.kind = kind
        self.touch(window_id)
        if opened:
            self._tell_window("opened", record)

    def detached(self, connection_id: str) -> None:
        """A connection closed; the window on it is gone."""
        for record in self._windows.values():
            if record.connection_id == connection_id and record.connected:
                record.connected = False
                if self._active == record.id:
                    self._active = None
                self._tell_window("closed", record)

    def touch(self, window_id: str) -> None:
        if window_id in self._windows:
            self._active = window_id
            self._last_used[window_id] = next(self._tick)

    def touch_connection(self, connection_id: str) -> None:
        window = self.by_connection(connection_id)
        if window is not None:
            self.touch(window)

    def written(self, window_id: str, key: str, value: str) -> None:
        """The window wrote its own state (``set_client_state``)."""
        if window_id not in self._windows:
            # A write from a window that has not hydrated in this
            # process — a reconnect racing its own hydrate. It is open.
            self.attached(window_id)
        self.touch(window_id)
        if self._seen.get((window_id, key), "") == value:
            return
        self._tell_state(window_id, key, value)

    # ── Reading ───────────────────────────────────────────────────

    def by_connection(self, connection_id: str) -> str | None:
        for record in self._windows.values():
            if record.connection_id == connection_id and record.connected:
                return record.id
        return None

    def open(self) -> list[WindowRecord]:
        return [r for r in self._windows.values() if r.connected]

    def active(self) -> str | None:
        """The window the person last used, of those still open."""
        if self._active is not None and self._windows[self._active].connected:
            return self._active
        open_windows = self.open()
        if not open_windows:
            return None
        return max(open_windows, key=lambda r: self._last_used.get(r.id, -1)).id

    def known(self, window_id: str) -> bool:
        record = self._windows.get(window_id)
        return record is not None and record.connected

    def state(self, window_id: str) -> dict[str, str]:
        return self._store.get_for_client(window_id)

    def describe(self, window_id: str) -> dict[str, Any]:
        record = self._windows[window_id]
        return record.describe(self.state(window_id))

    def describe_all(self) -> list[dict[str, Any]]:
        return [r.describe(self.state(r.id)) for r in self.open()]

    # ── Writing ───────────────────────────────────────────────────

    def write(self, window_id: str, key: str, value: str) -> None:
        """Change a window's state for it, and tell it.

        The same store the window writes, so a reload restores what the
        extension set; the push is what makes a live window apply it.
        """
        if value:
            self._store.set_value(window_id, key, value)
        else:
            self._store.delete_value(window_id, key)
        self._push(window_id, key, value)
        self._tell_state(window_id, key, value)

    # ── Values ────────────────────────────────────────────────────

    def route(self, window_id: str) -> list[dict[str, Any]]:
        raw = self.state(window_id).get(UiKeys.PAGE) or ""
        try:
            parsed = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        pages: list[dict[str, Any]] = []
        for entry in parsed:
            if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
                break
            params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
            pages.append(
                {
                    "kind": entry["kind"],
                    "label": str(entry.get("label") or entry["kind"]),
                    "params": {str(k): str(v) for k, v in params.items()},
                }
            )
        return pages

    def sidebar(self, window_id: str) -> dict[str, Any]:
        raw = self.state(window_id).get(UiKeys.SIDEBAR)
        return {"open": raw == "true", "width": None, "section": None}

    def session(self, window_id: str) -> str | None:
        return self.state(window_id).get(UiKeys.SESSION) or None

    def draft(self, window_id: str) -> str:
        session = self.session(window_id)
        if not session:
            return ""
        return self.state(window_id).get(UiKeys.draft(session)) or ""

    # ── Internals ─────────────────────────────────────────────────

    def _tell_state(self, window_id: str, key: str, value: str) -> None:
        self._seen[(window_id, key)] = value
        for listener in list(self._state_listeners):
            try:
                listener(window_id, key, value)
            except Exception:
                logger.exception("host model: a window-state listener failed")

    def _tell_window(self, what: str, record: WindowRecord) -> None:
        described = record.describe(self.state(record.id))
        for listener in list(self._window_listeners):
            try:
                listener(what, described)
            except Exception:
                logger.exception("host model: a window listener failed")


__all__ = ["UiKeys", "WindowRecord", "Windows"]
