"""igni's windows, and the model each one's views bind to.

A window is a view of igni — a desktop window, an IDE panel, a browser
tab. Each has a stable id that survives reloads: the ``client_id`` the
web client keeps (``clientState.ts``'s ``ensureClientId``).

**The window's model.** Everything a window shows — its page stack,
the panel over its chat, its sidebar, the preview, the selection, the
composer, toasts, the dialog, where its transcript is scrolled, whether
it has focus, and which session it is bound to — is a field of
:class:`WindowModel`, owned here. The web client holds nothing of its
own: it hydrates from this on connect (the ``window_model`` RPC), binds
its views to the copy it keeps, and writes back through the same RPC;
an extension writes through ``host.ui``. Both reach :meth:`Windows.write`
(or :meth:`Windows.toast`, :meth:`Windows.open_dialog`, …), so a change
is the same change whoever made it: stored, pushed to every window as
``window_model_changed``, and told to the host model, which signals it.

**Order is the backend's.** Writes are applied in the order they reach
this process — one event loop, one sequence — and each push carries the
window's revision and the writer's token. A window shows its own write
at once and keeps showing it until its echo arrives; whatever arrives
last from here is what every window shows. ``clientState.ts`` documents
the client's half.

**What survives what.** The page stack, the sidebar, the bound session
and folder, and each session's draft and composer mode are written to
the :class:`~ember_code.core.session.client_state.ClientStateStore`, so
they survive igni restarting. The rest — panel, preview, selection,
toasts, dialog, scroll, focus — lives as long as this backend does,
and survives a window reloading: a dialog an extension is waiting on is
still there when the window comes back.

**The active window** is the one the person last did something in.
``host.ui`` addresses it. The *focused* window is the one that says it
has focus; when one does, it is also the active one.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ember_code.extensions.host.types import SchemaTypes

logger = logging.getLogger(__name__)


class UiKeys:
    """The keys a window's lasting fields are stored under."""

    SESSION = "session-id"
    PROJECT_DIR = "project-dir"
    PAGE = "page"
    SIDEBAR = "sidebar-open"
    SIDEBAR_WIDTH = "sidebar-width"
    SIDEBAR_SECTION = "sidebar-section"
    DRAFT_PREFIX = "draft:"
    MODE_PREFIX = "draft-mode:"

    @classmethod
    def draft(cls, session_id: str) -> str:
        return f"{cls.DRAFT_PREFIX}{session_id}"

    @classmethod
    def mode(cls, session_id: str) -> str:
        return f"{cls.MODE_PREFIX}{session_id}"


class WindowField(BaseModel):
    """One field of a window's model: its schema type, and whether it lasts."""

    model_config = ConfigDict(frozen=True)

    #: The host schema's type expression for the field's value.
    type: str
    #: Written to the client-state store, so it survives a restart.
    lasting: bool = False
    #: A view may write it with ``write``. Toasts and the dialog change
    #: through their own operations, so a stale list can never drop one
    #: somebody else just added.
    writable: bool = True


#: Every field of a window's model. ``ui.<name>`` in the host schema
#: for all but ``session`` and ``project_dir`` (``sessions.current``)
#: and ``focused`` (``windows.focused``).
FIELDS: dict[str, WindowField] = {
    "session": WindowField(type="str", lasting=True),
    "project_dir": WindowField(type="str", lasting=True),
    "route": WindowField(type="list[Page]", lasting=True),
    "panel": WindowField(type="Panel | None"),
    "sidebar": WindowField(type="Sidebar", lasting=True),
    "preview": WindowField(type="Preview | None"),
    "selection": WindowField(type="SelectionState"),
    "composer": WindowField(type="Composer", lasting=True),
    "toasts": WindowField(type="list[Toast]", writable=False),
    "dialog": WindowField(type="Dialog | None", writable=False),
    "scroll": WindowField(type="Scroll"),
    "focused": WindowField(type="bool"),
}

#: The composer's modes.
MODES = ("chat", "command", "shell")

#: An ``@path`` reference in a draft — what ``attachments`` lists. The
#: composer's inline code pills (``@code:<id>``) are not files.
_REFERENCE = re.compile(r"(?:^|(?<=\s))@(?!code:)(\S+)")

#: How many toasts a window holds; the oldest goes first.
MAX_TOASTS = 6


class Transient(BaseModel):
    """The fields of one window that live in this process only."""

    panel: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    selection: dict[str, Any] = Field(
        default_factory=lambda: {"message_id": None, "text": None, "file": None}
    )
    toasts: list[dict[str, Any]] = Field(default_factory=list)
    dialog: dict[str, Any] | None = None
    scroll: dict[str, Any] = Field(default_factory=lambda: {"message_id": None, "at_bottom": True})
    focused: bool = False


class WindowRecord(BaseModel):
    id: str
    kind: str = "web"
    connection_id: str = ""
    connected: bool = True


class WindowChange(BaseModel):
    """One change to one window's model, as listeners are told it."""

    window: str
    field: str
    value: Any = None
    #: The window's revision after the change.
    rev: int
    #: The writer's token, echoed so the window that wrote it knows its
    #: own write; empty for an extension's.
    token: str = ""
    #: For the list- and dialog-shaped fields: what happened —
    #: ``toast_shown``, ``toast_dismissed``, ``dialog_opened``,
    #: ``dialog_closed`` — and to which one.
    event: str = ""
    item: dict[str, Any] | None = None
    answer: Any = None


class WindowRequest(BaseModel):
    """One ``window_model`` request: a window reading or writing its model.

    ``hydrate`` answers the whole model and says the window is open;
    ``write`` sets one field; ``toast`` and ``dismiss_toast`` add and
    remove one toast; ``open_dialog`` shows igni's own dialog and
    ``answer_dialog`` closes the open one with the person's answer
    (``value``; ``None`` for dismissed). ``token`` is the window's own
    name for the write, echoed on the push.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["hydrate", "write", "toast", "dismiss_toast", "open_dialog", "answer_dialog"]
    window: str = Field(min_length=1)
    field: str = ""
    value: Any = None
    token: str = ""
    dialog_id: str = ""
    connection_id: str = ""
    kind: str = ""


class WindowModelError(ValueError):
    """A write the model refuses: an unknown field, or a value that does
    not fit the field's schema type."""


ChangeListener = Callable[[WindowChange], None]
WindowListener = Callable[[str, dict[str, Any]], None]
Push = Callable[[WindowChange], None]


def references(text: str) -> list[str]:
    """The ``@path`` references in a draft, in order, without repeats."""
    seen: list[str] = []
    for match in _REFERENCE.finditer(text):
        path = match.group(1)
        if path not in seen:
            seen.append(path)
    return seen


class Windows:
    """Every window this backend has seen, and the model of each."""

    def __init__(self, store: Any, *, types: SchemaTypes, push: Push) -> None:
        self._store = store
        self._types = types
        self._push = push
        self._windows: dict[str, WindowRecord] = {}
        self._transient: dict[str, Transient] = {}
        self._revs: dict[str, int] = {}
        self._active: str | None = None
        self._focused: str | None = None
        self._tick = itertools.count()
        self._last_used: dict[str, int] = {}
        self._dialogs: dict[str, asyncio.Future[Any]] = {}
        self._change_listeners: list[ChangeListener] = []
        self._window_listeners: list[WindowListener] = []

    # ── Listeners ─────────────────────────────────────────────────

    def on_change(self, listener: ChangeListener) -> None:
        self._change_listeners.append(listener)

    def on_window(self, listener: WindowListener) -> None:
        """``listener("opened" | "closed" | "focus_changed", window)``."""
        self._window_listeners.append(listener)

    # ── Windows coming and going ──────────────────────────────────

    def attached(self, window_id: str, *, connection_id: str = "", kind: str = "") -> None:
        """A window hydrated its model on connect: it is open."""
        if not window_id:
            return
        record = self._windows.get(window_id)
        opened = record is None or not record.connected
        if record is None:
            record = WindowRecord(id=window_id)
            self._windows[window_id] = record
        record.connected = True
        if connection_id:
            record.connection_id = connection_id
        if kind:
            record.kind = kind
        self.touch(window_id)
        if opened:
            self._tell_window("opened", window_id)

    def detached(self, connection_id: str) -> None:
        """A connection closed; the window on it is gone."""
        for record in self._windows.values():
            if record.connection_id == connection_id and record.connected:
                record.connected = False
                if self._active == record.id:
                    self._active = None
                if self._focused == record.id:
                    self._focused = None
                    self._ui(record.id).focused = False
                self._tell_window("closed", record.id)

    def touch(self, window_id: str) -> None:
        if window_id in self._windows:
            self._active = window_id
            self._last_used[window_id] = next(self._tick)

    def touch_connection(self, connection_id: str) -> None:
        window = self.by_connection(connection_id)
        if window is not None:
            self.touch(window)

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

    def focused(self) -> str | None:
        return self._focused

    def known(self, window_id: str) -> bool:
        record = self._windows.get(window_id)
        return record is not None and record.connected

    def rev(self, window_id: str) -> int:
        return self._revs.get(window_id, 0)

    def describe(self, window_id: str) -> dict[str, Any]:
        record = self._windows[window_id]
        return {
            "id": record.id,
            "kind": record.kind,
            "connected": record.connected,
            "focused": self._focused == record.id,
            "session_id": self.session(record.id) or None,
            "project_dir": self._stored(record.id).get(UiKeys.PROJECT_DIR) or None,
        }

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(r.id) for r in self.open()]

    def snapshot(self, window_id: str) -> dict[str, Any]:
        """Every field of a window's model — what a window hydrates from."""
        return {name: self.get(window_id, name) for name in FIELDS}

    def unset(self, window_id: str) -> list[str]:
        """Lasting fields this window has never written: the window
        applies its own defaults to them (a wide window opens with its
        sidebar out, a narrow one without)."""
        stored = self._stored(window_id)
        return [] if UiKeys.SIDEBAR in stored else ["sidebar"]

    def get(self, window_id: str, name: str) -> Any:
        stored = self._stored(window_id)
        ui = self._ui(window_id)
        if name == "session":
            return stored.get(UiKeys.SESSION, "")
        if name == "project_dir":
            return stored.get(UiKeys.PROJECT_DIR, "")
        if name == "route":
            return self._route(stored.get(UiKeys.PAGE, ""))
        if name == "sidebar":
            return self._sidebar(stored)
        if name == "composer":
            return self._composer(stored)
        if name == "focused":
            return ui.focused
        if name in FIELDS:
            return getattr(ui, name)
        raise WindowModelError(f"a window's model has no field {name!r}")

    def route(self, window_id: str) -> list[dict[str, Any]]:
        return list(self.get(window_id, "route"))

    def sidebar(self, window_id: str) -> dict[str, Any]:
        return dict(self.get(window_id, "sidebar"))

    def session(self, window_id: str) -> str | None:
        return self._stored(window_id).get(UiKeys.SESSION) or None

    def composer(self, window_id: str) -> dict[str, Any]:
        return dict(self.get(window_id, "composer"))

    # ── Writing ───────────────────────────────────────────────────

    def write(self, window_id: str, name: str, value: Any, *, token: str = "") -> WindowChange:
        """Set one field of a window's model — the view's write and an
        extension's alike. Validated against the field's schema type;
        a write of the value already there is not a change."""
        spec = FIELDS.get(name)
        if spec is None:
            raise WindowModelError(f"a window's model has no field {name!r}")
        if not spec.writable:
            raise WindowModelError(f"{name!r} changes through its own operations, not a write")
        value = self._valid(name, spec.type, value)
        if name == "composer":
            value = self._normal_composer(value)
        if name == "sidebar":
            value = self._normal_sidebar(value)
        if name == "route":
            value = [self._normal_page(p) for p in value]
        if name == "scroll":
            value.pop("window", None)
        if name == "panel" and value is not None:
            value["params"] = value.get("params") or {}
        if self.get(window_id, name) == value and name != "route":
            # The same route twice is still news: "take me there" from
            # a link to the page already open returns it to its top.
            return WindowChange(window=window_id, field=name, value=value, rev=self.rev(window_id))
        self._set(window_id, name, value)
        if name == "focused":
            self._focus(window_id, bool(value))
        else:
            self.touch(window_id)
        change = self._changed(window_id, name, self.get(window_id, name), token=token)
        if name == "session":
            # The draft is the session's: binding another session shows
            # another draft.
            self._changed(window_id, "composer", self.get(window_id, "composer"), token=token)
        return change

    def toast(self, window_id: str, toast: dict[str, Any], *, token: str = "") -> dict[str, Any]:
        """Show a toast. Its id is the host's, so two writers cannot collide."""
        if toast.get("target"):
            toast = {**toast, "target": self._normal_page(toast["target"])}
        shown = self._valid("toast", "Toast", {**toast, "id": f"t-{uuid.uuid4().hex[:12]}"})
        shown["kind"] = shown.get("kind") or "info"
        ui = self._ui(window_id)
        while len(ui.toasts) >= MAX_TOASTS:
            # The oldest goes, and says so: a toast nobody saw leave is
            # one an extension would wait on.
            self.dismiss_toast(window_id, str(ui.toasts[0]["id"]), token=token)
        ui.toasts = [*ui.toasts, shown]
        self._changed(
            window_id, "toasts", list(ui.toasts), token=token, event="toast_shown", item=shown
        )
        return shown

    def dismiss_toast(
        self, window_id: str, toast_id: str, *, token: str = ""
    ) -> dict[str, Any] | None:
        ui = self._ui(window_id)
        gone = next((t for t in ui.toasts if t["id"] == toast_id), None)
        if gone is None:
            return None
        ui.toasts = [t for t in ui.toasts if t["id"] != toast_id]
        self._changed(
            window_id, "toasts", list(ui.toasts), token=token, event="toast_dismissed", item=gone
        )
        return gone

    def toast_of(self, window_id: str, toast_id: str) -> dict[str, Any] | None:
        return next((t for t in self._ui(window_id).toasts if t["id"] == toast_id), None)

    def open_dialog(
        self, window_id: str, spec: Any, *, source: str | None = None, token: str = ""
    ) -> tuple[dict[str, Any], asyncio.Future[Any]]:
        """Show a dialog; the future is the person's answer.

        One dialog at a time: the one already open closes unanswered
        (``None``), as if dismissed. Deterministic, and nobody waits on
        a dialog that can no longer be answered.
        """
        ui = self._ui(window_id)
        if ui.dialog is not None:
            self.answer_dialog(window_id, str(ui.dialog["id"]), None, token=token)
        dialog = {"id": f"d-{uuid.uuid4().hex[:12]}", "spec": spec, "source": source}
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._dialogs[dialog["id"]] = future
        ui.dialog = dialog
        self.touch(window_id)
        self._changed(window_id, "dialog", dialog, token=token, event="dialog_opened", item=dialog)
        return dialog, future

    def answer_dialog(
        self, window_id: str, dialog_id: str, answer: Any, *, token: str = ""
    ) -> bool:
        """The person answered (or dismissed, ``None``). ``False`` if that
        dialog is not the one open — answered already, or replaced."""
        ui = self._ui(window_id)
        if ui.dialog is None or ui.dialog["id"] != dialog_id:
            return False
        closed = ui.dialog
        ui.dialog = None
        future = self._dialogs.pop(dialog_id, None)
        if future is not None and not future.done():
            future.set_result(answer)
        self._changed(
            window_id,
            "dialog",
            None,
            token=token,
            event="dialog_closed",
            item=closed,
            answer=answer,
        )
        return True

    def cancel_dialogs(self) -> None:
        """Shutting down: nobody will answer."""
        for future in self._dialogs.values():
            if not future.done():
                future.set_result(None)
        self._dialogs.clear()

    # ── Internals ─────────────────────────────────────────────────

    def _ui(self, window_id: str) -> Transient:
        found = self._transient.get(window_id)
        if found is None:
            found = Transient()
            self._transient[window_id] = found
        return found

    def _stored(self, window_id: str) -> dict[str, str]:
        result: dict[str, str] = self._store.get_for_client(window_id)
        return result

    def _store_value(self, window_id: str, key: str, value: str) -> None:
        if value:
            self._store.set_value(window_id, key, value)
        else:
            self._store.delete_value(window_id, key)

    def _valid(self, name: str, expression: str, value: Any) -> Any:
        try:
            validated = self._types.validate(expression, _unset_nones(value))
        except ValidationError as exc:
            raise WindowModelError(f"{name}: expected {expression}: {exc}") from None
        return _plain(validated)

    def _set(self, window_id: str, name: str, value: Any) -> None:
        if name == "session":
            self._store_value(window_id, UiKeys.SESSION, str(value))
        elif name == "project_dir":
            self._store_value(window_id, UiKeys.PROJECT_DIR, str(value))
        elif name == "route":
            self._store_value(window_id, UiKeys.PAGE, json.dumps(value) if value else "")
        elif name == "sidebar":
            self._store_value(window_id, UiKeys.SIDEBAR, "true" if value["open"] else "false")
            width = value.get("width")
            self._store_value(window_id, UiKeys.SIDEBAR_WIDTH, str(width) if width else "")
            self._store_value(window_id, UiKeys.SIDEBAR_SECTION, value.get("section") or "")
        elif name == "composer":
            session = self.session(window_id)
            if not session:
                raise WindowModelError(
                    f"window {window_id} shows no session yet: no draft to write"
                )
            self._store_value(window_id, UiKeys.draft(session), value["text"])
            mode = value.get("mode")
            self._store_value(
                window_id, UiKeys.mode(session), "" if mode in (None, "chat") else mode
            )
        else:
            setattr(self._ui(window_id), name, value)

    def _focus(self, window_id: str, focused: bool) -> None:
        before = self._focused
        if focused:
            if before is not None and before != window_id:
                self._ui(before).focused = False
                self._changed(before, "focused", False)
            self._focused = window_id
            self.touch(window_id)
        elif before == window_id:
            self._focused = None
        if self._focused != before:
            self._tell_window("focus_changed", window_id)

    def _changed(
        self,
        window_id: str,
        name: str,
        value: Any,
        *,
        token: str = "",
        event: str = "",
        item: dict[str, Any] | None = None,
        answer: Any = None,
    ) -> WindowChange:
        rev = self._revs.get(window_id, 0) + 1
        self._revs[window_id] = rev
        change = WindowChange(
            window=window_id,
            field=name,
            value=value,
            rev=rev,
            token=token,
            event=event,
            item=item,
            answer=answer,
        )
        try:
            self._push(change)
        except Exception:
            logger.exception("host model: pushing a window change failed")
        for listener in list(self._change_listeners):
            try:
                listener(change)
            except Exception:
                logger.exception("host model: a window-change listener failed")
        return change

    def _tell_window(self, what: str, window_id: str) -> None:
        described = self.describe(window_id)
        for listener in list(self._window_listeners):
            try:
                listener(what, described)
            except Exception:
                logger.exception("host model: a window listener failed")

    # ── Values, from what is stored ───────────────────────────────

    @staticmethod
    def _normal_page(page: dict[str, Any]) -> dict[str, Any]:
        params = page.get("params") or {}
        return {
            "kind": str(page["kind"]),
            "label": str(page.get("label") or page["kind"]),
            "params": {str(k): str(v) for k, v in params.items()},
        }

    @classmethod
    def _route(cls, raw: str) -> list[dict[str, Any]]:
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
            if not isinstance(entry.get("params"), dict):
                entry = {**entry, "params": {}}
            pages.append(cls._normal_page(entry))
        return pages

    @staticmethod
    def _normal_sidebar(value: dict[str, Any]) -> dict[str, Any]:
        width = value.get("width")
        if width is not None and not 180 <= int(width) <= 600:
            raise WindowModelError(f"sidebar width {width} is outside 180 to 600 pixels")
        section = value.get("section")
        if section not in (None, "links", "sessions"):
            raise WindowModelError(f"sidebar section {section!r} is not `links` or `sessions`")
        return {"open": bool(value.get("open")), "width": width, "section": section}

    @staticmethod
    def _sidebar(stored: dict[str, str]) -> dict[str, Any]:
        width = stored.get(UiKeys.SIDEBAR_WIDTH) or ""
        return {
            "open": stored.get(UiKeys.SIDEBAR) == "true",
            "width": int(width) if width.isdigit() else None,
            "section": stored.get(UiKeys.SIDEBAR_SECTION) or None,
        }

    @staticmethod
    def _normal_composer(value: dict[str, Any]) -> dict[str, Any]:
        mode = value.get("mode") or "chat"
        if mode not in MODES:
            raise WindowModelError(f"composer mode {mode!r} is not one of {', '.join(MODES)}")
        text = str(value.get("text") or "")
        return {"text": text, "attachments": references(text), "mode": mode}

    @staticmethod
    def _composer_from(text: str, mode: str) -> dict[str, Any]:
        return {"text": text, "attachments": references(text), "mode": mode or "chat"}

    def _composer(self, stored: dict[str, str]) -> dict[str, Any]:
        session = stored.get(UiKeys.SESSION, "")
        if not session:
            return self._composer_from("", "chat")
        return self._composer_from(
            stored.get(UiKeys.draft(session), ""), stored.get(UiKeys.mode(session), "")
        )


def _unset_nones(value: Any) -> Any:
    """A key holding ``None`` as a key left out. Every optional field of
    the schema's types defaults to ``None``, so this changes no meaning —
    and a value read back from the model (where an absent part is
    ``None``) can be written again as it was read."""
    if isinstance(value, dict):
        return {k: _unset_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_unset_nones(v) for v in value]
    return value


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


__all__ = [
    "FIELDS",
    "MODES",
    "UiKeys",
    "WindowChange",
    "WindowModelError",
    "WindowRecord",
    "WindowRequest",
    "Windows",
    "references",
]
