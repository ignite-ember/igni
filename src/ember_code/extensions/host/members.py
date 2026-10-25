"""Every built member of the host object model, against igni's backend.

One class per object, one method per member, each tagged with what it
implements — the same shape :class:`~ember_code.backend.rpc_handlers.base.RpcHandler`
gives the RPC table, for the same reason: the table is built by walking
the classes, so a member cannot be implemented twice or under a typo.

Each member reaches igni the way igni's own UI does: the RPC handler a
panel calls, the action a window sends, the store a window writes.
Nothing here re-implements what igni does; it translates between the
model's promise and igni's internal wire, which is free to change.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ember_code import __version__
from ember_code.core.decisions import DecisionRequest, DecisionsUnavailable, DecisionTooLong
from ember_code.extensions.host.errors import HostModelError
from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.host.spec import ErrorCode
from ember_code.extensions.host.windows import WindowModelError, references
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.extensions.host.model import HostModel

_TAG = "_host_member"


@dataclass
class Call:
    """One request, resolved: who asks, about which window and session."""

    extension_id: str
    grants: HostGrants
    #: The window a per-window member is about, or the caller's window.
    window: str | None
    #: The session the caller's chat is: its window's, else the default.
    session_id: str
    params: dict[str, Any]
    value: Any = None


Impl = Callable[[Call], Awaitable[Any]]

_F = TypeVar("_F", bound=Callable[..., Any])


def get(path: str) -> Callable[[_F], _F]:
    return _tag("get", path)


def put(path: str) -> Callable[[_F], _F]:
    return _tag("set", path)


def call(path: str) -> Callable[[_F], _F]:
    return _tag("call", path)


def _tag(op: str, path: str) -> Callable[[_F], _F]:
    def decorate(fn: _F) -> _F:
        setattr(fn, _TAG, (op, path))
        return fn

    return decorate


class Members:
    """Base: the model, and the table of this class's members."""

    def __init__(self, model: HostModel) -> None:
        self.model = model
        self.backend = model.backend

    def table(self) -> dict[tuple[str, str], Impl]:
        out: dict[tuple[str, str], Impl] = {}
        for name in dir(type(self)):
            if name.startswith("__"):
                continue
            attr = getattr(type(self), name, None)
            key = getattr(attr, _TAG, None)
            if key is None:
                continue
            if key in out:
                raise RuntimeError(f"host member {key} implemented twice")
            out[key] = getattr(self, name)
        return out

    async def rpc(self, method: RpcMethod, args: dict[str, Any] | None = None, *, c: Call) -> Any:
        return await self.model.rpc(method, args or {}, session_id=c.session_id)


# ── host.app ──────────────────────────────────────────────────────


class AppMembers(Members):
    async def display_value(self, session_id: str) -> dict[str, Any]:
        config = await self.model.rpc(RpcMethod.GET_DISPLAY_CONFIG, {}, session_id=session_id)
        styles = await self.model.rpc(RpcMethod.GET_OUTPUT_STYLES, {}, session_id=session_id)
        return {
            "verbose": bool(config.get("show_routing", False)),
            "output_style": str(styles.get("active") or ""),
            "output_styles": [str(s.get("name", "")) for s in styles.get("styles") or []],
            "color_theme": str(config.get("color_theme", "auto")),
            "show_reasoning": bool(config.get("show_reasoning", False)),
            "show_tool_calls": bool(config.get("show_tool_calls", True)),
        }

    @get("app.display")
    async def display(self, c: Call) -> Any:
        return await self.display_value(c.session_id)

    @get("app.version")
    async def version(self, c: Call) -> Any:
        return __version__

    @get("app.update_available")
    async def update_available(self, c: Call) -> Any:
        return self.model.state.update

    @get("app.connection")
    async def connection(self, c: Call) -> Any:
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return {
            "cloud_connected": bool(status.get("cloud_connected")),
            "model_configured": bool(status.get("model_configured", True)),
        }

    @call("app.set_verbose")
    async def set_verbose(self, c: Call) -> Any:
        current = await self.display_value(c.session_id)
        if current["verbose"] != bool(c.params["on"]):
            # ``app.display_changed`` follows from the toggle itself.
            await self.rpc(RpcMethod.TOGGLE_VERBOSE, c=c)
        return await self.display_value(c.session_id)

    @call("app.set_output_style")
    async def set_output_style(self, c: Call) -> Any:
        name = str(c.params["name"]).strip()
        if not name or any(ch.isspace() for ch in name):
            raise HostModelError(ErrorCode.INVALID, f"{name!r} is not an output style name")
        outcome = await self.model.command(f"/output-style {name}", session_id=c.session_id)
        if outcome.get("kind") == "error":
            raise HostModelError(ErrorCode.REFUSED, str(outcome.get("content") or "refused"))
        display = await self.display_value(c.session_id)
        self.model.emit("app.display_changed", display)
        return display

    @call("app.check_for_update")
    async def check_for_update(self, c: Call) -> Any:
        found = await self.rpc(RpcMethod.CHECK_FOR_UPDATE, c=c)
        update = (
            {
                "current_version": found["current_version"],
                "latest_version": found["latest_version"],
                "download_url": found["download_url"],
            }
            if isinstance(found, dict) and found.get("available")
            else None
        )
        self.model.state.update = update
        self.model.emit("app.update_changed", update)
        return update

    @call("app.open_external")
    async def open_external(self, c: Call) -> Any:
        if c.window is None:
            raise HostModelError(ErrorCode.NOT_FOUND, "no igni window is open")
        try:
            link = ExternalLink(url=c.params["url"])
        except ValidationError as exc:
            raise HostModelError(ErrorCode.INVALID, exc.errors()[0]["msg"]) from None
        self.model.open_external(c.window, link.url, source=c.extension_id)


class ExternalLink(BaseModel):
    """A link an extension asks igni to open in the person's browser.

    Absolute ``https`` only, with a host and no credentials: nothing
    that runs on this machine (``file:``, a custom scheme), nothing
    readable in transit, and nothing that hides who it signs in as.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _https(cls, url: str) -> str:
        if any(ch.isspace() or ord(ch) < 0x20 for ch in url):
            raise ValueError("a link has no whitespace or control characters")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError(f"only https links open outside igni, not {parts.scheme or url!r}")
        if not parts.hostname:
            raise ValueError("a link names its host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("a link carries no credentials")
        return url


class AppStateMembers(Members):
    """An extension's own keys, in the store a window's state lives in."""

    @staticmethod
    def client_id(c: Call) -> str:
        # Its own client id: no window and no other extension can read
        # or write these keys, and these keys cannot reach a window's.
        return f"ext:{c.extension_id}"

    @get("app.state.values")
    async def values(self, c: Call) -> Any:
        return self.backend.client_state.get_for_client(self.client_id(c))

    @call("app.state.get")
    async def get_value(self, c: Call) -> Any:
        return self.backend.client_state.get_for_client(self.client_id(c)).get(c.params["key"])

    @call("app.state.set")
    async def set_value(self, c: Call) -> Any:
        key = str(c.params["key"])
        if not key:
            raise HostModelError(ErrorCode.INVALID, "a state key cannot be empty")
        self.backend.client_state.set_value(self.client_id(c), key, str(c.params["value"]))
        self.model.emit(
            "app.state.changed",
            {"key": key, "value": str(c.params["value"])},
            only=c.extension_id,
        )

    @call("app.state.delete")
    async def delete_value(self, c: Call) -> Any:
        key = str(c.params["key"])
        self.backend.client_state.delete_value(self.client_id(c), key)
        self.model.emit("app.state.changed", {"key": key, "value": None}, only=c.extension_id)


# ── host.windows, host.ui ─────────────────────────────────────────


class WindowMembers(Members):
    @get("windows.list")
    async def window_list(self, c: Call) -> Any:
        return self.model.windows.describe_all()

    @get("windows.focused")
    async def focused(self, c: Call) -> Any:
        return self.model.windows.focused()


class UiMembers(Members):
    """``host.ui``: one window's model, written through the same
    :class:`~ember_code.extensions.host.windows.Windows` the window
    itself writes — so an extension's change is pushed to the window,
    and signalled, exactly as the person's is."""

    def window(self, c: Call) -> str:
        if c.window is None:
            raise HostModelError(ErrorCode.NOT_FOUND, "no igni window is open")
        return c.window

    def read(self, c: Call, name: str) -> Any:
        return self.model.windows.get(self.window(c), name)

    def write(self, c: Call, name: str, value: Any) -> Any:
        window = self.window(c)
        try:
            self.model.windows.write(window, name, value)
        except WindowModelError as exc:
            raise HostModelError(ErrorCode.INVALID, str(exc)) from None
        return self.model.windows.get(window, name)

    # ── route ─────────────────────────────────────────────────────

    @get("ui.route")
    async def route(self, c: Call) -> Any:
        return self.read(c, "route")

    @put("ui.route")
    async def set_route(self, c: Call) -> Any:
        self.write(c, "route", c.value or [])

    @call("ui.navigate")
    async def navigate(self, c: Call) -> Any:
        page = {
            "kind": c.params["page"],
            "label": c.params["page"],
            "params": c.params.get("params") or {},
        }
        return self.write(c, "route", [page])

    @call("ui.back")
    async def back(self, c: Call) -> Any:
        return self.write(c, "route", self.read(c, "route")[:-1])

    # ── panel ─────────────────────────────────────────────────────

    @get("ui.panel")
    async def panel(self, c: Call) -> Any:
        return self.read(c, "panel")

    @put("ui.panel")
    async def set_panel(self, c: Call) -> Any:
        self.write(c, "panel", c.value)

    @call("ui.open_panel")
    async def open_panel(self, c: Call) -> Any:
        kind = str(c.params["kind"])
        if kind not in PANELS:
            raise HostModelError(
                ErrorCode.INVALID, f"no panel {kind!r}: one of {', '.join(sorted(PANELS))}"
            )
        return self.write(c, "panel", {"kind": kind, "params": c.params.get("params") or {}})

    @call("ui.close_panel")
    async def close_panel(self, c: Call) -> Any:
        self.write(c, "panel", None)

    # ── sidebar ───────────────────────────────────────────────────

    @get("ui.sidebar")
    async def sidebar(self, c: Call) -> Any:
        return self.read(c, "sidebar")

    @put("ui.sidebar")
    async def set_sidebar_value(self, c: Call) -> Any:
        self.write(c, "sidebar", c.value)

    @call("ui.set_sidebar")
    async def set_sidebar(self, c: Call) -> Any:
        current = self.read(c, "sidebar")
        width = c.params.get("width")
        section = c.params.get("section")
        return self.write(
            c,
            "sidebar",
            {
                "open": bool(c.params["open"]),
                "width": current.get("width") if width is None else width,
                "section": current.get("section") if section is None else section,
            },
        )

    # ── preview ───────────────────────────────────────────────────

    @get("ui.preview")
    async def preview(self, c: Call) -> Any:
        return self.read(c, "preview")

    @put("ui.preview")
    async def set_preview(self, c: Call) -> Any:
        if c.value is not None:
            self.within_workspace(c, str(c.value.get("path", "")))
        self.write(c, "preview", c.value)

    @call("ui.open_preview")
    async def open_preview(self, c: Call) -> Any:
        path = str(c.params["path"])
        self.within_workspace(c, path)
        return self.write(c, "preview", {"path": path})

    @call("ui.close_preview")
    async def close_preview(self, c: Call) -> Any:
        self.write(c, "preview", None)

    def within_workspace(self, c: Call, path: str) -> None:
        """A preview shows a file's contents to the person, not to the
        extension — but it is still the extension pointing at the
        project, so the path must be one ``workspacePaths`` allows."""
        if not c.grants.workspace.granted or not c.grants.workspace.permits(path):
            raise HostModelError(
                ErrorCode.REFUSED,
                f"'{path}' is outside what '{c.extension_id}' was granted of the workspace",
            )

    # ── selection ─────────────────────────────────────────────────

    @get("ui.selection")
    async def selection(self, c: Call) -> Any:
        return self.read(c, "selection")

    @put("ui.selection")
    async def set_selection(self, c: Call) -> Any:
        self.write(c, "selection", c.value)

    @call("ui.select")
    async def select(self, c: Call) -> Any:
        return self.write(c, "selection", c.params["selection"])

    # ── composer ──────────────────────────────────────────────────

    @get("ui.composer")
    async def composer(self, c: Call) -> Any:
        return self.read(c, "composer")

    @put("ui.composer")
    async def set_composer_value(self, c: Call) -> Any:
        self.write(c, "composer", c.value)

    @call("ui.set_composer")
    async def set_composer(self, c: Call) -> Any:
        text = str(c.params["text"])
        for path in c.params.get("attachments") or []:
            if path not in references(text):
                sep = "" if not text or text.endswith((" ", "\n")) else " "
                text = f"{text}{sep}@{path} "
        mode = c.params.get("mode") or self.read(c, "composer").get("mode") or "chat"
        return self.write(c, "composer", {"text": text, "mode": mode})

    # ── toasts ────────────────────────────────────────────────────

    @get("ui.toasts")
    async def toasts(self, c: Call) -> Any:
        return self.read(c, "toasts")

    @call("ui.toast")
    async def toast(self, c: Call) -> Any:
        kind = c.params.get("kind") or "info"
        if kind not in TOAST_KINDS:
            raise HostModelError(
                ErrorCode.INVALID, f"toast kind {kind!r}: one of {', '.join(TOAST_KINDS)}"
            )
        return self.model.windows.toast(
            self.window(c),
            {
                "text": c.params["text"],
                "kind": kind,
                "body": c.params.get("body"),
                "target": c.params.get("target"),
                "source": c.extension_id,
            },
        )

    @call("ui.dismiss_toast")
    async def dismiss_toast(self, c: Call) -> Any:
        window = self.window(c)
        toast_id = str(c.params["toast_id"])
        found = self.model.windows.toast_of(window, toast_id)
        if found is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no toast {toast_id!r} is on screen")
        if found.get("source") != c.extension_id:
            raise HostModelError(ErrorCode.REFUSED, "an extension dismisses only its own toasts")
        self.model.windows.dismiss_toast(window, toast_id)

    # ── dialog ────────────────────────────────────────────────────

    @get("ui.dialog")
    async def dialog(self, c: Call) -> Any:
        return self.read(c, "dialog")

    @call("ui.show_dialog")
    async def show_dialog(self, c: Call) -> Any:
        spec = DialogSpec.of(c.params["spec"])
        _dialog, answer = self.model.windows.open_dialog(
            self.window(c), spec, source=c.extension_id
        )
        return await answer

    # ── scroll ────────────────────────────────────────────────────

    @get("ui.scroll")
    async def scroll(self, c: Call) -> Any:
        return self.read(c, "scroll")

    @call("ui.scroll_to")
    async def scroll_to(self, c: Call) -> Any:
        target = str(c.params["message_id"])
        if target == "bottom":
            return self.write(c, "scroll", {"message_id": None, "at_bottom": True})
        return self.write(c, "scroll", {"message_id": target, "at_bottom": False})


#: The panels a window has, by kind.
PANELS = frozenset({"notifications", "search", "login", "dir-picker"})

#: The kinds of toast.
TOAST_KINDS = ("info", "success", "warning", "error")


class DialogButton(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    primary: bool = False


class DialogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ""
    placeholder: str = ""
    value: str = ""


class DialogSpec(BaseModel):
    """What an extension may show in a dialog. Text only: igni draws it."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    body: str = ""
    buttons: list[DialogButton] = Field(
        default_factory=lambda: [DialogButton(id="ok", label="OK", primary=True)],
        min_length=1,
        max_length=4,
    )
    input: DialogInput | None = None

    @classmethod
    def of(cls, value: Any) -> dict[str, Any]:
        try:
            spec = cls.model_validate(value)
        except ValidationError as exc:
            raise HostModelError(ErrorCode.INVALID, f"ui.show_dialog(spec): {exc}") from None
        return {"kind": "extension", **spec.model_dump(mode="json")}


# ── host.sessions ─────────────────────────────────────────────────


class SessionMembers(Members):
    @get("sessions.list")
    async def session_list(self, c: Call) -> Any:
        result = await self.rpc(RpcMethod.LIST_SESSIONS, c=c)
        sessions = result.get("sessions", []) if isinstance(result, dict) else result
        return [self.model.session_info(s) for s in sessions or []]

    @get("sessions.current")
    async def current(self, c: Call) -> Any:
        return c.session_id

    @put("sessions.current")
    async def set_current(self, c: Call) -> Any:
        await self.switch(c, str(c.value or ""))

    @call("sessions.open")
    async def open(self, c: Call) -> Any:
        return await self.switch(c, str(c.params["session_id"]))

    async def switch(self, c: Call, session_id: str) -> dict[str, Any]:
        if not session_id:
            raise HostModelError(ErrorCode.INVALID, "a session id is required")
        if c.window is None:
            # No window is open: the TUI's way, which switches the boot
            # session itself.
            info = await self.model.rpc(
                RpcMethod.SWITCH_SESSION, {"session_id": session_id}, session_id=c.session_id
            )
            if isinstance(info, dict) and info.get("type") == "error":
                raise HostModelError(ErrorCode.REFUSED, str(info.get("text") or "refused"))
            self.model.session_switched(session_id, None)
            return {"session_id": session_id, "window": None}
        # A window binds a session as the window does: attach (which
        # refuses another project's session), then the window's state.
        attached = await self.model.attach(session_id, None)
        window = c.window
        # The window's model says which session it shows; the write is
        # the signal (``session.switched``) and what moves the window.
        self.model.windows.write(window, "session", attached["session_id"])
        return {"session_id": attached["session_id"], "window": window}

    @call("sessions.create")
    async def create(self, c: Call) -> Any:
        project_dir = c.params.get("project_dir") or str(self.backend.project_dir)
        attached = await self.model.attach("", project_dir)
        if c.window is not None:
            # The folder first: a window moving to the session reads it.
            self.model.windows.write(c.window, "project_dir", attached["project_dir"])
            self.model.windows.write(c.window, "session", attached["session_id"])
        return attached

    @call("sessions.attach")
    async def attach(self, c: Call) -> Any:
        return await self.model.attach(str(c.params["session_id"]), c.params.get("project_dir"))


# ── host.chat ─────────────────────────────────────────────────────


class ChatMembers(Members):
    @get("chat.session_id")
    async def session_id(self, c: Call) -> Any:
        return c.session_id

    @get("chat.messages")
    async def messages(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_CHAT_HISTORY, {"session_id": c.session_id}, c=c)

    @get("chat.pending")
    async def pending(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_PENDING_MESSAGES, {"session_id": c.session_id}, c=c)

    @get("chat.processing")
    async def processing(self, c: Call) -> Any:
        return bool(await self.rpc(RpcMethod.GET_PROCESSING, c=c))

    @get("chat.status")
    async def status(self, c: Call) -> Any:
        return self.model.status_of(await self.rpc(RpcMethod.GET_STATUS, c=c), c.session_id)

    @get("chat.timeout")
    async def timeout(self, c: Call) -> Any:
        return int(await self.rpc(RpcMethod.GET_RUN_TIMEOUT, c=c))

    @get("chat.interrupted_runs")
    async def interrupted_runs(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_INTERRUPTED_RUNS, {"session_id": c.session_id}, c=c)

    @get("chat.context")
    async def context(self, c: Call) -> Any:
        tokens = await self.rpc(RpcMethod.COUNT_CONTEXT_TOKENS, c=c)
        breakdown = await self.rpc(RpcMethod.GET_CONTEXT_BREAKDOWN, c=c)
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return {
            "tokens": int(tokens or 0),
            "max": int(status.get("max_context") or 0),
            "breakdown": breakdown,
        }

    @call("chat.post")
    async def post(self, c: Call) -> Any:
        text = str(c.params["text"])
        if not text.strip():
            raise HostModelError(ErrorCode.INVALID, "an empty message is not posted")
        await self.backend.post(
            session_id=c.session_id,
            extension_id=c.extension_id,
            text=text,
            media=c.params.get("attachments") or None,
            queued=False,
        )

    @call("chat.queue")
    async def queue(self, c: Call) -> Any:
        text = str(c.params["text"])
        if not text.strip():
            raise HostModelError(ErrorCode.INVALID, "an empty message is not queued")
        await self.backend.post(
            session_id=c.session_id,
            extension_id=c.extension_id,
            text=text,
            media=None,
            queued=True,
        )

    @call("chat.append")
    async def append(self, c: Call) -> Any:
        view_id = f"ext-{uuid.uuid4().hex[:12]}"
        await self.show(c, view_id, c.params["view"], str(c.params.get("title") or ""))
        self.model.state.view_owner[view_id] = c.extension_id
        return view_id

    @call("chat.replace")
    async def replace(self, c: Call) -> Any:
        view_id = str(c.params["view_id"])
        owner = self.model.state.view_owner.get(view_id)
        if owner is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no view {view_id!r} was appended")
        if owner != c.extension_id:
            raise HostModelError(
                ErrorCode.REFUSED, f"view {view_id!r} belongs to another extension"
            )
        await self.show(c, view_id, c.params["view"], str(c.params.get("title") or ""))

    async def show(self, c: Call, view_id: str, spec: Any, title: str) -> None:
        if not isinstance(spec, dict) or not isinstance(spec.get("root"), str):
            raise HostModelError(
                ErrorCode.INVALID,
                "a view is a json-render spec: {root: <id>, elements: {<id>: {type, props, children}}}",
            )
        payload = {
            "type": "visualization_delta",
            "agent_path": f"ext:{c.extension_id}",
            "spec_id": view_id,
            "json": json.dumps(spec),
            "final": True,
            "title": title,
            "source": c.extension_id,
        }
        await self.backend.show_view(
            session_id=c.session_id,
            payload=payload,
            run_id=self.model.state.last_run.get(c.session_id, ""),
        )

    @call("chat.search")
    async def search(self, c: Call) -> Any:
        limit = c.params.get("limit") or 50
        return await self.rpc(
            RpcMethod.SEARCH_CHAT,
            {"session_id": c.session_id, "query": c.params["query"], "limit": limit},
            c=c,
        )

    @call("chat.cancel")
    async def cancel(self, c: Call) -> Any:
        await self.rpc(RpcMethod.CANCEL_RUN, c=c)

    @call("chat.cancel_agent")
    async def cancel_agent(self, c: Call) -> Any:
        await self.rpc(RpcMethod.CANCEL_AGENT_RUN, {"run_id": c.params["run_id"]}, c=c)

    @call("chat.truncate")
    async def truncate(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.TRUNCATE_HISTORY,
            {"session_id": c.session_id, "run_id": c.params["run_id"]},
            c=c,
        )

    @call("chat.compact")
    async def compact(self, c: Call) -> Any:
        tokens = int(await self.rpc(RpcMethod.COUNT_CONTEXT_TOKENS, c=c) or 0)
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        cleared = await self.rpc(
            RpcMethod.COMPACT_IF_NEEDED,
            {"ctx_tokens": tokens, "max_ctx": int(status.get("max_context") or 0)},
            c=c,
        )
        if not cleared:
            return None
        return {
            "session_id": c.session_id,
            "new_session_id": str(cleared.get("new_session_id", "")),
            "summary": str(cleared.get("summary", "")),
        }

    @call("chat.extract_learnings")
    async def extract_learnings(self, c: Call) -> Any:
        await self.rpc(
            RpcMethod.EXTRACT_LEARNINGS,
            {"user_msg": c.params["user_msg"], "assistant_msg": c.params["assistant_msg"]},
            c=c,
        )

    @call("chat.discard_interrupted")
    async def discard_interrupted(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.DISCARD_INTERRUPTED_RUN,
            {"session_id": c.session_id, "message_id": c.params["message_id"]},
            c=c,
        )

    @call("chat.upload")
    async def upload(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.UPLOAD_ATTACHMENT,
            {"filename": c.params["filename"], "content_base64": c.params["content_base64"]},
            c=c,
        )


class ApprovalMembers(Members):
    @get("chat.approvals.waiting")
    async def waiting(self, c: Call) -> Any:
        return list(self.model.state.approvals.get(c.session_id, {}).values())

    @call("chat.approvals.answer")
    async def answer(self, c: Call) -> Any:
        requirement_id = str(c.params["requirement_id"])
        self._check(c, requirement_id, str(c.params["action"]))
        await self.backend.act(
            msg.HITLResponse(
                requirement_id=requirement_id,
                action=str(c.params["action"]),
                choice=str(c.params.get("choice") or ""),
            ),
            session_id=c.session_id,
        )

    @call("chat.approvals.answer_all")
    async def answer_all(self, c: Call) -> Any:
        decisions = []
        for answer in c.params["answers"]:
            self._check(c, str(answer["requirement_id"]), str(answer["action"]))
            decisions.append(
                msg.HITLDecision(
                    requirement_id=str(answer["requirement_id"]),
                    action=str(answer["action"]),
                    choice=str(answer.get("choice") or ""),
                )
            )
        await self.backend.act(msg.HITLResponseBatch(decisions=decisions), session_id=c.session_id)

    def _check(self, c: Call, requirement_id: str, action: str) -> None:
        if action not in ("confirm", "reject"):
            raise HostModelError(
                ErrorCode.INVALID, f"action is 'confirm' or 'reject', not {action!r}"
            )
        if requirement_id not in self.model.state.approvals.get(c.session_id, {}):
            raise HostModelError(
                ErrorCode.NOT_FOUND,
                f"no request {requirement_id!r} is waiting in session {c.session_id}",
            )


class ViewMembers(Members):
    @get("chat.views.list")
    async def view_list(self, c: Call) -> Any:
        return self.backend.views(c.session_id)

    @call("chat.views.dispatch")
    async def dispatch(self, c: Call) -> Any:
        params = dict(c.params.get("params") or {})
        params["view_id"] = c.params["view_id"]
        return await self.rpc(
            RpcMethod.DISPATCH_VISUALIZATION_ACTION,
            {"action": c.params["action"], "params": params},
            c=c,
        )


# ── plan, tasks, loop ─────────────────────────────────────────────


class PlanMembers(Members):
    @get("plan.latest")
    async def latest(self, c: Call) -> Any:
        found = await self.rpc(RpcMethod.GET_LATEST_PLAN, c=c)
        return {
            "latest": str(found.get("latest", "")),
            "history": list(found.get("history") or []),
            "tasks": list(found.get("tasks") or []),
            "state": str(found.get("state", "")),
        }

    @call("plan.approve")
    async def approve(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.APPROVE_PLAN, {"run_id": c.params.get("run_id") or ""}, c=c)

    @call("plan.dismiss")
    async def dismiss(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.DISMISS_PLAN, {"run_id": c.params.get("run_id") or ""}, c=c)


class TaskMembers(Members):
    @get("tasks.todos")
    async def todos(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_TODOS, c=c)


class LoopMembers(Members):
    @get("loop.status")
    async def status(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.LOOP_STATUS, c=c)

    async def _then_status(self, c: Call, method: RpcMethod) -> Any:
        # ``loop.changed`` is emitted for every loop write, the person's
        # and this one, by :meth:`BroadcastSignals.after_rpc`.
        return await self.rpc(method, c=c)

    @call("loop.pause")
    async def pause(self, c: Call) -> Any:
        return bool(await self._then_status(c, RpcMethod.LOOP_PAUSE))

    @call("loop.resume")
    async def resume(self, c: Call) -> Any:
        return str(await self._then_status(c, RpcMethod.LOOP_RESUME) or "")

    @call("loop.cancel_pending")
    async def cancel_pending(self, c: Call) -> Any:
        return bool(await self._then_status(c, RpcMethod.CANCEL_PENDING_LOOP))

    @call("loop.pop_pending")
    async def pop_pending(self, c: Call) -> Any:
        return await self._then_status(c, RpcMethod.POP_PENDING_LOOP_ITERATION)


# ── commands, agents, workflows, scheduler, processes ─────────────


class CommandMembers(Members):
    @get("commands.list")
    async def command_list(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SLASH_COMMANDS, c=c)

    @get("commands.skills")
    async def skills(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SKILL_NAMES, c=c)

    @get("commands.skill_definitions")
    async def skill_definitions(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SKILL_DEFINITIONS, c=c)

    @get("commands.skill_details")
    async def skill_details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SKILL_DETAILS, c=c)

    @call("commands.run")
    async def run(self, c: Call) -> Any:
        command = str(c.params["command"]).strip()
        if not command.startswith("/"):
            raise HostModelError(ErrorCode.INVALID, f"{command!r} is not a slash command")
        return await self.model.command(command, session_id=c.session_id)


class AgentMembers(Members):
    @get("agents.details")
    async def details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_AGENT_DETAILS, c=c)

    @call("agents.promote_ephemeral")
    async def promote(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.PROMOTE_EPHEMERAL_AGENT, {"name": c.params["name"]}, c=c)

    @call("agents.discard_ephemeral")
    async def discard(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.DISCARD_EPHEMERAL_AGENT, {"name": c.params["name"]}, c=c)


class WorkflowMembers(Members):
    @get("workflows.list")
    async def workflow_list(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.LIST_WORKFLOWS, c=c)

    @call("workflows.run")
    async def run(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.RUN_WORKFLOW,
            {
                "name": c.params["name"],
                "args": c.params.get("input") or {},
                "session_id": c.session_id,
            },
            c=c,
        )

    @call("workflows.cancel")
    async def cancel(self, c: Call) -> Any:
        found = await self.rpc(
            RpcMethod.CANCEL_WORKFLOW, {"workflow_run_id": c.params["workflow_run_id"]}, c=c
        )
        return bool(found.get("cancelled")) if isinstance(found, dict) else bool(found)


class SchedulerMembers(Members):
    @get("scheduler.tasks")
    async def tasks(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_SCHEDULED_TASKS, {"include_done": True}, c=c)

    @call("scheduler.start")
    async def start(self, c: Call) -> Any:
        await self.rpc(RpcMethod.START_SCHEDULER, c=c)

    @call("scheduler.run_now")
    async def run_now(self, c: Call) -> Any:
        task_id = str(c.params["task_id"])
        tasks = await self.rpc(RpcMethod.GET_SCHEDULED_TASKS, {"include_done": True}, c=c)
        task = next(
            (t for t in tasks or [] if str(t.get("id") or t.get("task_id")) == task_id), None
        )
        if task is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no scheduled task {task_id!r}")
        return str(
            await self.rpc(
                RpcMethod.EXECUTE_SCHEDULED_TASK, {"description": task.get("description", "")}, c=c
            )
        )

    @call("scheduler.cancel")
    async def cancel(self, c: Call) -> Any:
        await self.rpc(RpcMethod.CANCEL_SCHEDULED_TASK, {"task_id": c.params["task_id"]}, c=c)


class ProcessMembers(Members):
    @get("processes.list")
    async def process_list(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.LIST_BACKGROUND_PROCESSES, c=c)

    @call("processes.tail")
    async def tail(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.READ_PROCESS_TAIL,
            {"pid": c.params["pid"], "tail": c.params.get("lines") or 200},
            c=c,
        )

    @call("processes.stop")
    async def stop(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.STOP_BACKGROUND_PROCESS, {"pid": c.params["pid"]}, c=c)

    @call("processes.run")
    async def run(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.RUN_SHELL, {"command": c.params["command"]}, c=c)


# ── workspace ─────────────────────────────────────────────────────


class WorkspaceMembers(Members):
    def root(self, c: Call) -> Path:
        return self.backend.project_dir_of(c.session_id)

    def inside(self, c: Call, path: str, *, directory: bool = False) -> str:
        """``path`` relative to the project, if the grant reaches it."""
        root = self.root(c).resolve()
        target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if not target.is_relative_to(root):
            raise HostModelError(ErrorCode.REFUSED, f"{path!r} is outside the project")
        relative = target.relative_to(root).as_posix()
        if relative == "." or not relative:
            if directory:
                return ""
            raise HostModelError(ErrorCode.INVALID, "a file path is required")
        if not directory and not c.grants.workspace.permits(relative):
            raise HostModelError(
                ErrorCode.NOT_GRANTED,
                f"{relative!r} is outside '{c.extension_id}''s workspacePaths",
            )
        return relative

    @get("workspace.root")
    async def root_value(self, c: Call) -> Any:
        return str(self.root(c))

    @get("workspace.head")
    async def head(self, c: Call) -> Any:
        return self.model.head(self.root(c))

    @call("workspace.read")
    async def read(self, c: Call) -> Any:
        relative = self.inside(c, str(c.params["path"]))
        return await self.rpc(RpcMethod.READ_FILE, {"path": relative}, c=c)

    @call("workspace.list")
    async def list_dir(self, c: Call) -> Any:
        relative = self.inside(c, str(c.params.get("dir") or "."), directory=True)
        found = await self.rpc(RpcMethod.LIST_DIRS, {"path": str(self.root(c) / relative)}, c=c)
        if isinstance(found, dict) and isinstance(found.get("dirs"), list):
            prefix = f"{relative}/" if relative else ""
            found["dirs"] = [
                d for d in found["dirs"] if c.grants.workspace.permits(f"{prefix}{d}/x")
            ]
        return found

    @call("workspace.complete")
    async def complete(self, c: Call) -> Any:
        found = await self.rpc(
            RpcMethod.COMPLETE_FILES, {"query": c.params["prefix"], "limit": 50}, c=c
        )
        if isinstance(found, dict):
            found["matches"] = [
                m for m in found.get("matches", []) if c.grants.workspace.permits(str(m))
            ]
            found["total"] = len(found["matches"])
        return found

    @call("workspace.search")
    async def search(self, c: Call) -> Any:
        found = await self.rpc(RpcMethod.SEARCH_CODE, {"query": c.params["query"]}, c=c)
        if isinstance(found, dict):
            found["matches"] = [
                m
                for m in found.get("matches", [])
                if c.grants.workspace.permits(str(m.get("path", "")))
            ]
        return found

    @call("workspace.pick_dir")
    async def pick_dir(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.PICK_DIR_NATIVE, {}, c=c)


# ── models, mcp, plugins, extensions, hooks, permissions ──────────


class ModelMembers(Members):
    @get("models.registry")
    async def registry(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MODEL_REGISTRY, c=c)

    @get("models.current")
    async def current(self, c: Call) -> Any:
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return str(status.get("model", ""))

    @put("models.current")
    async def set_current(self, c: Call) -> Any:
        name = str(c.value or "")
        registry = await self.rpc(RpcMethod.GET_MODEL_REGISTRY, c=c)
        if name not in (registry.get("registry") or {}):
            raise HostModelError(ErrorCode.REFUSED, f"no model {name!r} is configured")
        info = await self.rpc(RpcMethod.SWITCH_MODEL, {"model_name": name}, c=c)
        if isinstance(info, dict) and info.get("type") == "error":
            raise HostModelError(ErrorCode.REFUSED, str(info.get("text") or "refused"))


class McpMembers(Members):
    @get("mcp.servers")
    async def servers(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MCP_SERVERS, c=c)

    @get("mcp.status")
    async def status(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MCP_STATUS, c=c)

    @get("mcp.details")
    async def details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MCP_SERVER_DETAILS, c=c)

    @call("mcp.connect")
    async def connect(self, c: Call) -> Any:
        await self.rpc(RpcMethod.MCP_CONNECT, {"server_name": c.params["name"]}, c=c)

    @call("mcp.disconnect")
    async def disconnect(self, c: Call) -> Any:
        await self.rpc(RpcMethod.MCP_DISCONNECT, {"server_name": c.params["name"]}, c=c)

    @call("mcp.ensure")
    async def ensure(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.ENSURE_MCP, c=c)

    @call("mcp.set_tool_enabled")
    async def set_tool_enabled(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.SET_MCP_TOOL_ENABLED,
            {
                "server": c.params["server"],
                "tool": c.params["tool"],
                "enabled": bool(c.params["on"]),
            },
            c=c,
        )


class PluginMembers(Members):
    @get("plugins.installed")
    async def installed(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_PLUGIN_DETAILS, c=c)

    @get("plugins.marketplaces")
    async def marketplaces(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_MARKETPLACES, c=c)

    @call("plugins.contents")
    async def contents(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_PLUGIN_CONTENTS, {"name": c.params["name"]}, c=c)

    @call("plugins.preview")
    async def preview(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.PREVIEW_PLUGIN,
            {
                "source": c.params["source"],
                "branch": c.params.get("branch"),
                "subdir": c.params.get("subdir"),
            },
            c=c,
        )

    async def _info(self, c: Call, method: RpcMethod, args: dict[str, Any]) -> None:
        info = await self.rpc(method, args, c=c)
        if isinstance(info, dict) and info.get("type") == "error":
            raise HostModelError(ErrorCode.REFUSED, str(info.get("text") or "refused"))

    @call("plugins.install")
    async def install(self, c: Call) -> Any:
        await self._info(
            c,
            RpcMethod.INSTALL_PLUGIN,
            {"ref": c.params["ref"], "install_ref": c.params.get("install_ref")},
        )

    @call("plugins.update")
    async def update(self, c: Call) -> Any:
        await self._info(
            c,
            RpcMethod.UPDATE_PLUGIN,
            {"name": c.params["name"], "install_ref": c.params.get("install_ref")},
        )

    @call("plugins.remove")
    async def remove(self, c: Call) -> Any:
        await self._info(c, RpcMethod.REMOVE_PLUGIN, {"name": c.params["name"]})

    @call("plugins.set_enabled")
    async def set_enabled(self, c: Call) -> Any:
        await self._info(
            c,
            RpcMethod.SET_PLUGIN_ENABLED,
            {"name": c.params["name"], "enabled": bool(c.params["on"])},
        )

    @call("plugins.add_marketplace")
    async def add_marketplace(self, c: Call) -> Any:
        await self._info(c, RpcMethod.ADD_MARKETPLACE, {"url": c.params["url"]})

    @call("plugins.remove_marketplace")
    async def remove_marketplace(self, c: Call) -> Any:
        await self._info(c, RpcMethod.REMOVE_MARKETPLACE, {"name": c.params["name"]})

    @call("plugins.refresh_marketplaces")
    async def refresh_marketplaces(self, c: Call) -> Any:
        await self._info(c, RpcMethod.REFRESH_MARKETPLACES, {"name": c.params.get("name")})


class ExtensionMembers(Members):
    @get("extensions.installed")
    async def installed(self, c: Call) -> Any:
        return self.model.extensions_installed()

    @call("extensions.set_settings")
    async def set_settings(self, c: Call) -> Any:
        return await self.model.save_settings(c.extension_id, dict(c.params["values"]))


class SelfMembers(Members):
    @get("self.id")
    async def own_id(self, c: Call) -> Any:
        return c.extension_id

    @get("self.settings")
    async def settings(self, c: Call) -> Any:
        return self.model.settings_of(c.extension_id)

    @put("self.settings")
    async def set_settings(self, c: Call) -> Any:
        if not isinstance(c.value, dict):
            raise HostModelError(ErrorCode.INVALID, "settings are an object of key to value")
        await self.model.save_settings(c.extension_id, dict(c.value))

    @get("self.state")
    async def state(self, c: Call) -> Any:
        return self.model.extension_info(c.extension_id)

    @get("self.web_url")
    async def web_url(self, c: Call) -> Any:
        return self.model.extension_info(c.extension_id).get("web_url")


class HookMembers(Members):
    @get("hooks.details")
    async def details(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_HOOKS_DETAILS, c=c)

    @call("hooks.reload")
    async def reload(self, c: Call) -> Any:
        await self.rpc(RpcMethod.RELOAD_HOOKS, c=c)

    @call("hooks.fire_session_start")
    async def fire_session_start(self, c: Call) -> Any:
        await self.rpc(RpcMethod.FIRE_SESSION_START_HOOK, c=c)


class PermissionMembers(Members):
    @get("permissions.mode")
    async def mode(self, c: Call) -> Any:
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return str(status.get("permission_mode", "default"))

    @call("permissions.check")
    async def check(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.CHECK_PERMISSION,
            {
                "tool_name": c.params["tool_name"],
                "func_name": c.params["func_name"],
                "tool_args": c.params.get("tool_args") or {},
            },
            c=c,
        )

    @call("permissions.save_rule")
    async def save_rule(self, c: Call) -> Any:
        level = str(c.params["level"])
        if level not in ("allow", "ask", "deny"):
            raise HostModelError(ErrorCode.INVALID, f"level is allow, ask or deny — not {level!r}")
        await self.rpc(
            RpcMethod.SAVE_PERMISSION_RULE, {"rule": c.params["rule"], "level": level}, c=c
        )


class PolicyMembers(Members):
    @get("policy.pack")
    async def pack(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_GROUP_POLICY, c=c)


class AccountMembers(Members):
    @get("account.status")
    async def status(self, c: Call) -> Any:
        return await self.model.account(c.session_id)

    @call("account.sign_in")
    async def sign_in(self, c: Call) -> Any:
        # Starts the person's own sign-in. What it produces — a URL, a
        # code, a session — is theirs, so nothing of it is returned.
        await self.rpc(RpcMethod.LOGIN, c=c)

    @call("account.sign_out")
    async def sign_out(self, c: Call) -> Any:
        await self.rpc(RpcMethod.CLEAR_CLOUD_CREDENTIALS, c=c)
        return await self.model.account(c.session_id)


class NotificationMembers(Members):
    @get("notifications.list")
    async def notification_list(self, c: Call) -> Any:
        found = await self.rpc(RpcMethod.NOTIFICATIONS_LIST, c=c)
        rows = found.get("notifications", []) if isinstance(found, dict) else []
        mine = [self.model.notification(r) for r in rows]
        if c.grants.allows("notifications"):
            return mine
        return [n for n in mine if n["source"] == c.extension_id]

    @get("notifications.unread")
    async def unread(self, c: Call) -> Any:
        counts = await self.rpc(RpcMethod.NOTIFICATIONS_COUNTS, c=c)
        return self.model.counts_for(c.grants, counts)

    @call("notifications.notify")
    async def notify(self, c: Call) -> Any:
        self.model.notify(c.extension_id, c.params["notification"])

    @call("notifications.mark_read")
    async def mark_read(self, c: Call) -> Any:
        ids = c.params.get("ids")
        source = c.params.get("source")
        if not c.grants.allows("notifications"):
            # Its own only: a source other than itself is refused, and
            # "everything" means everything of its own.
            if source not in (None, c.extension_id):
                raise HostModelError.not_granted(
                    c.extension_id, "marking another's notifications", "notifications"
                )
            if ids:
                listing = await self.rpc(RpcMethod.NOTIFICATIONS_LIST, c=c)
                own = {
                    r.get("id")
                    for r in listing.get("notifications", [])
                    if r.get("source") == c.extension_id
                }
                foreign = [i for i in ids if i not in own]
                if foreign:
                    raise HostModelError.not_granted(
                        c.extension_id, "marking another's notifications", "notifications"
                    )
            else:
                source = c.extension_id
        found = await self.rpc(
            RpcMethod.NOTIFICATIONS_MARK_READ, {"ids": ids, "source": source}, c=c
        )
        counts = found.get("counts", {}) if isinstance(found, dict) else {}
        return self.model.counts_for(c.grants, counts)


# ── host.decide ───────────────────────────────────────────────────


class DecisionMembers(Members):
    """Typed questions to igni's local decision model, behind the Bypass guard."""

    @call("decide")
    async def decide(self, c: Call) -> Any:
        decisions = await self._decide([c.params["context"]], c.params["questions"])
        return decisions[0]

    @call("decide_batch")
    async def decide_batch(self, c: Call) -> Any:
        return await self._decide(c.params["contexts"], c.params["questions"])

    async def _decide(self, contexts: list[Any], questions: list[Any]) -> list[dict[str, Any]]:
        try:
            request = DecisionRequest(contexts=contexts, questions=questions)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise HostModelError(
                ErrorCode.INVALID, f"{where}: {first['msg']}" if where else first["msg"]
            ) from None
        try:
            decided = await self.model.decisions.decide(request)
        except DecisionTooLong as exc:
            raise HostModelError(ErrorCode.INVALID, str(exc)) from None
        except DecisionsUnavailable as exc:
            raise HostModelError(ErrorCode.UNAVAILABLE, str(exc)) from None
        return [decision.model_dump(mode="json") for decision in decided]


ALL_MEMBERS: tuple[type[Members], ...] = (
    AppMembers,
    AppStateMembers,
    WindowMembers,
    UiMembers,
    SessionMembers,
    ChatMembers,
    ApprovalMembers,
    ViewMembers,
    PlanMembers,
    TaskMembers,
    LoopMembers,
    CommandMembers,
    AgentMembers,
    WorkflowMembers,
    SchedulerMembers,
    ProcessMembers,
    WorkspaceMembers,
    ModelMembers,
    McpMembers,
    PluginMembers,
    ExtensionMembers,
    SelfMembers,
    HookMembers,
    PermissionMembers,
    PolicyMembers,
    AccountMembers,
    NotificationMembers,
    DecisionMembers,
)

__all__ = ["ALL_MEMBERS", "Call", "Members"]
