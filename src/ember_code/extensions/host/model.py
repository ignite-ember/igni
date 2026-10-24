"""The host object model, served.

:class:`HostModel` answers an extension's requests — ``get``, ``set``,
``call``, ``subscribe`` — from both channels (the process's loopback
socket, :mod:`.channel`, and the page's bridge, the ``host_model`` RPC),
checks every one against the extension's grants and the schema, and
runs the member that implements it (:mod:`.members`). It also turns
what igni does into signals: every message igni sends a window, every
write a window makes, every extension state change
(:mod:`.broadcasts`).

The schema is the authority. A member it does not define does not
exist; a stage-2 member answers that it is not yet available; a member
not granted answers ``not_granted`` and its signals never arrive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ember_code.backend.workspace_watch import GitWorkspace, Head
from ember_code.extensions.events import HostTopic
from ember_code.extensions.host.backend import HostBackend
from ember_code.extensions.host.broadcasts import BroadcastSignals
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.errors import HostModelError
from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.host.members import ALL_MEMBERS, Call, Impl
from ember_code.extensions.host.signals import Content, SignalBus, Subscriber
from ember_code.extensions.host.spec import (
    ErrorCode,
    HostSchema,
    MethodSpec,
    ObjectSpec,
    PropertySpec,
    SignalSpec,
)
from ember_code.extensions.host.types import SchemaTypes
from ember_code.extensions.host.windows import UiKeys, Windows
from ember_code.extensions.host.wire import ErrorBody, Op, Reply, Request, SignalMessage
from ember_code.extensions.registry import ExtensionRegistry, InstalledExtension
from ember_code.extensions.store import ExtensionStore
from ember_code.extensions.web_home import ExtensionWebHome
from ember_code.protocol import messages as msg
from ember_code.protocol.rpc import RpcMethod

logger = logging.getLogger(__name__)


@dataclass
class HostState:
    """What the host has observed and the backend does not keep."""

    #: The last update check's answer.
    update: dict[str, Any] | None = None
    #: Approvals waiting, by session, by requirement id.
    approvals: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    #: The last top-level run of each session.
    last_run: dict[str, str] = field(default_factory=dict)
    #: Files the current top-level run of each session edited.
    run_files: dict[str, set[str]] = field(default_factory=dict)
    #: Live drafts, by window, from the windows' typing.
    drafts: dict[str, str] = field(default_factory=dict)
    #: Which extension appended which view.
    view_owner: dict[str, str] = field(default_factory=dict)
    #: The last status seen, by session.
    status: dict[str, dict[str, Any]] = field(default_factory=dict)


class HostModel:
    """Every extension's view of igni, through one schema."""

    def __init__(
        self,
        *,
        backend: HostBackend,
        registry: ExtensionRegistry,
        store: ExtensionStore | None = None,
        notifications: Any = None,
        notices: Any = None,
        schema: HostSchema = HOST_SCHEMA,
        strict: bool = False,
    ) -> None:
        """
        Args:
            notifications: The floor's
                :class:`~ember_code.notifications.center.NotificationCenter`,
                when there is one.
            notices: An
                :class:`~ember_code.notifications.extension_notices.ExtensionNoticeReceiver`
                for ``host.notifications.notify`` — the same check a notice
                arriving over MCP passes.
            strict: Check every answer against the schema and fail loudly
                when it does not fit. For tests: in production an answer
                is sent as the backend gave it.
        """
        self.schema = schema
        self.backend = backend
        self.registry = registry
        self.store = store if store is not None else ExtensionStore()
        self.web_home = ExtensionWebHome(self.store)
        self.notifications = notifications
        self.notices = notices
        self.strict = strict
        self.types = SchemaTypes(schema)
        self.state = HostState()
        self.bus = SignalBus(schema, self.grants_for)
        self.windows = Windows(backend.client_state, push=self._push_client_state)
        self.windows.on_state(self._window_state_changed)
        self.windows.on_window(self._window_changed)
        self.broadcasts = BroadcastSignals(self)
        self._impl: dict[tuple[str, str], Impl] = {}
        for members_class in ALL_MEMBERS:
            for key, impl in members_class(self).table().items():
                if key in self._impl:
                    raise RuntimeError(f"host member {key} implemented twice")
                self._impl[key] = impl
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pages: dict[str, Subscriber] = {}
        #: Told when a session is switched without a window (the TUI's
        #: way) — wired to :class:`~ember_code.backend.host_events.HostEvents`.
        self.on_session_switched: list[Any] = []

    # ── Grants ────────────────────────────────────────────────────

    def grants_for(self, extension_id: str) -> HostGrants | None:
        entry = self.registry.get(extension_id)
        if entry is None:
            return None
        return HostGrants(entry.manifest)

    def implemented(self) -> set[tuple[str, str]]:
        """``(op, member path)`` for every built member, for the coverage test."""
        return set(self._impl)

    # ── Requests ──────────────────────────────────────────────────

    async def handle(
        self,
        extension_id: str,
        request: Request,
        *,
        subscriber: Subscriber | None = None,
        home_window: str | None = None,
    ) -> Reply:
        """Answer one request. Never raises: a refusal is a reply.

        ``home_window`` is the window the caller lives in — a page's
        own window; ``None`` for a process, whose ``host.ui`` is the
        active window.
        """
        try:
            result = await self._handle(extension_id, request, subscriber, home_window)
        except HostModelError as exc:
            return Reply(
                id=request.id, ok=False, error=ErrorBody(code=exc.code, message=exc.message)
            )
        except Exception as exc:
            logger.exception("host model: %s %s failed", request.op.value, request.path)
            return Reply(
                id=request.id,
                ok=False,
                error=ErrorBody(code=ErrorCode.FAILED, message=f"{request.path}: {exc}"),
            )
        return Reply(id=request.id, ok=True, result=result)

    async def handle_page(self, extension_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """One request from an extension's page, through the bridge.

        ``home_window`` is the window the page is framed in — added by
        the window's side of the bridge, which knows it. A page's
        ``host.ui`` is its own window's.
        """
        home = args.pop("home_window", None)
        try:
            request = Request.model_validate({"id": "page", **args})
        except ValidationError as exc:
            return Reply(
                id=str(args.get("id", "page")),
                ok=False,
                error=ErrorBody(code=ErrorCode.INVALID, message=f"not a host-model request: {exc}"),
            ).model_dump(mode="json")
        subscriber = self._page_subscriber(extension_id)
        reply = await self.handle(
            extension_id,
            request,
            subscriber=subscriber,
            home_window=str(home) if home else None,
        )
        return reply.model_dump(mode="json")

    def _page_subscriber(self, extension_id: str) -> Subscriber:
        """One subscriber for all of an extension's pages.

        Its signals go to the windows as ``host_signal`` pushes addressed
        to the extension; each window posts them into that extension's
        frames, and the bridge in each frame calls the handlers it has.
        """
        found = self._pages.get(extension_id)
        if found is None:
            found = self.bus.attach(extension_id, _PageSink(self, extension_id))
            self._pages[extension_id] = found
        return found

    async def _handle(
        self,
        extension_id: str,
        request: Request,
        subscriber: Subscriber | None,
        home_window: str | None,
    ) -> Any:
        grants = self.grants_for(extension_id)
        if grants is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no extension {extension_id!r} is installed")
        if request.op in (Op.SUBSCRIBE, Op.UNSUBSCRIBE):
            if subscriber is None:
                raise HostModelError(ErrorCode.INVALID, "this channel does not take subscriptions")
            if request.op is Op.SUBSCRIBE:
                return dict(self.bus.subscribe(subscriber, request.signals))
            self.bus.unsubscribe(subscriber, request.signals)
            return None

        spec, member = self._resolve(request)
        path = request.path
        if member.stage != 1:
            raise HostModelError.stage_two(path)
        permission = spec.permission_of(member)
        if not grants.allows(permission):
            raise HostModelError.not_granted(extension_id, f"'{path}'", permission or "")

        window = self._window(spec, request, home_window)
        session_id = self._session_for(window)
        params: dict[str, Any] = {}
        value: Any = None
        if isinstance(member, MethodSpec):
            if request.op is not Op.CALL:
                raise HostModelError(ErrorCode.INVALID, f"'{path}' is a method: call it")
            params = self._params(path, member, request.params)
        elif isinstance(member, PropertySpec):
            if request.op is Op.CALL:
                raise HostModelError(ErrorCode.INVALID, f"'{path}' is a property: get or set it")
            if request.op is Op.SET:
                if not member.writable:
                    raise HostModelError(ErrorCode.READ_ONLY, f"'{path}' cannot be set")
                value = self._check(f"{path} =", member.type, request.value)
        else:
            raise HostModelError(
                ErrorCode.INVALID, f"'{path}' is a signal: subscribe to it with its wire name"
            )

        impl = self._impl.get((request.op.value, path))
        if impl is None:  # pragma: no cover — the coverage test forbids it
            raise HostModelError(ErrorCode.FAILED, f"'{path}' has no implementation")
        call = Call(
            extension_id=extension_id,
            grants=grants,
            window=window,
            session_id=session_id,
            params=params,
            value=value,
        )
        result = await impl(call)
        if self.strict and request.op is not Op.SET:
            expected = member.result if isinstance(member, MethodSpec) else member.type
            problem = self.types.check(expected, result)
            if problem is not None:
                raise AssertionError(
                    f"{path} answered outside its schema type {expected}: {problem}"
                )
        return result

    def _resolve(
        self, request: Request
    ) -> tuple[ObjectSpec, PropertySpec | SignalSpec | MethodSpec]:
        try:
            return self.schema.resolve(request.path)
        except KeyError:
            raise HostModelError(
                ErrorCode.NOT_FOUND, f"the host model has no member '{request.path}'"
            ) from None

    def _window(self, spec: ObjectSpec, request: Request, home: str | None) -> str | None:
        if request.window is not None:
            if not spec.per_window:
                raise HostModelError(
                    ErrorCode.INVALID, f"'{spec.path}' does not belong to a window"
                )
            if not self.windows.known(request.window):
                raise HostModelError(ErrorCode.NOT_FOUND, f"no window {request.window!r} is open")
            return request.window
        if home is not None and self.windows.known(home):
            return home
        return self.windows.active()

    def _session_for(self, window: str | None) -> str:
        if window is not None:
            bound = self.windows.session(window)
            if bound:
                return bound
        return self.backend.default_session()

    def _params(self, path: str, spec: MethodSpec, given: dict[str, Any]) -> dict[str, Any]:
        known = {p.name: p for p in spec.params}
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise HostModelError(ErrorCode.INVALID, f"{path} takes no {', '.join(unknown)}")
        out: dict[str, Any] = {}
        for name, param in known.items():
            if given.get(name) is None:
                if not param.optional:
                    raise HostModelError(ErrorCode.INVALID, f"{path}: '{name}' is required")
                out[name] = None
                continue
            out[name] = self._check(f"{path}({name})", param.type, given[name])
        return out

    def _check(self, where: str, expression: str, value: Any) -> Any:
        try:
            validated = self.types.validate(expression, value)
        except ValidationError as exc:
            raise HostModelError(
                ErrorCode.INVALID, f"{where}: expected {expression}: {exc}"
            ) from None
        return _plain(validated)

    # ── Signals ───────────────────────────────────────────────────

    def emit(
        self,
        wire: str,
        value: Any = None,
        *,
        content: Content | None = None,
        only: str | None = None,
    ) -> list[str]:
        return self.bus.emit(wire, value, content=content, only=only)

    def spawn(self, coro: Any) -> None:
        """Run something in the background, kept until it finishes."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background work; for tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── Reaching the backend ──────────────────────────────────────

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        """An RPC handler, as the UI calls it. Its failures are refusals."""
        try:
            result = await self.backend.rpc(method, args, session_id=session_id)
        except HostModelError:
            raise
        except KeyError as exc:
            raise HostModelError(ErrorCode.INVALID, f"{method.value}: {exc} is required") from None
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            raise HostModelError(ErrorCode.REFUSED, str(exc)) from None
        self.broadcasts.after_rpc(method, args, session_id, result)
        return result

    async def command(self, text: str, *, session_id: str) -> dict[str, Any]:
        """A slash command, answered here rather than in the windows."""
        answer = await self.backend.ask(msg.Command(text=text), session_id=session_id)
        return {
            "kind": str(getattr(answer, "kind", "info")),
            "content": str(getattr(answer, "content", "")),
            "display_content": str(getattr(answer, "display_content", "")),
        }

    async def attach(self, session_id: str, project_dir: str | None) -> dict[str, str]:
        try:
            return await self.backend.attach(session_id, project_dir)
        except ValueError as exc:
            raise HostModelError(ErrorCode.REFUSED, str(exc)) from None

    # ── Values the members share ──────────────────────────────────

    @staticmethod
    def session_info(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": str(entry.get("session_id", "")),
            "name": str(entry.get("name", "")),
            "created_at": int(entry.get("created_at") or 0),
            "updated_at": int(entry.get("updated_at") or 0),
            "run_count": int(entry.get("run_count") or 0),
            "summary": str(entry.get("summary", "")),
            "agent_name": str(entry.get("agent_name", "")),
        }

    @staticmethod
    def status_of(status: dict[str, Any], session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "model": str(status.get("model", "")),
            "model_configured": bool(status.get("model_configured", True)),
            "permission_mode": str(status.get("permission_mode", "default")),
            "input_tokens": int(status.get("input_tokens") or 0),
            "output_tokens": int(status.get("output_tokens") or 0),
            "context_tokens": int(status.get("context_tokens") or 0),
            "max_context": int(status.get("max_context") or 0),
        }

    @staticmethod
    def head(root: Path) -> dict[str, str] | None:
        head = GitWorkspace(root).head()
        return None if head is None else {"commit_sha": head.commit_sha, "branch": head.branch}

    async def account(self, session_id: str) -> dict[str, Any]:
        status = await self.backend.rpc(RpcMethod.GET_STATUS, {}, session_id=session_id)
        return self.account_of(status)

    @staticmethod
    def account_of(status: dict[str, Any]) -> dict[str, Any]:
        email = str(status.get("cloud_email") or "")
        return {
            "signed_in": bool(email) or bool(status.get("cloud_connected")),
            "email": email,
            "name": str(status.get("cloud_name") or ""),
            "org": str(status.get("cloud_org") or ""),
        }

    def extension_info(self, extension_id: str) -> dict[str, Any]:
        entry = self.registry.get(extension_id)
        if entry is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no extension {extension_id!r} is installed")
        return self._info(entry)

    def _info(self, entry: InstalledExtension) -> dict[str, Any]:
        manifest = entry.manifest
        return {
            "id": manifest.id,
            "name": manifest.display_name,
            "version": manifest.version,
            "state": entry.state.value,
            "reason": entry.reason,
            "web_url": self.web_home.url_for(manifest),
        }

    def extensions_installed(self) -> list[dict[str, Any]]:
        return [
            self._info(entry)
            for extension_id in self.registry.ids()
            if (entry := self.registry.get(extension_id)) is not None
        ]

    def settings_of(self, extension_id: str) -> dict[str, Any]:
        entry = self.registry.get(extension_id)
        if entry is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no extension {extension_id!r} is installed")
        return dict(self.store.settings(entry.manifest))

    async def save_settings(self, extension_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Through ``set_extension_settings``, as the Extensions page saves them."""
        await self.rpc(
            RpcMethod.SET_EXTENSION_SETTINGS,
            {"extension_id": extension_id, "values": values},
            session_id=self.backend.default_session(),
        )
        return {"values": self.settings_of(extension_id)}

    @staticmethod
    def notification(row: dict[str, Any]) -> dict[str, Any]:
        target = row.get("target")
        created = row.get("created_at")
        return {
            "id": str(row.get("id", "")),
            "origin": str(row.get("origin", "")),
            "source": str(row.get("source", "")),
            "kind": str(row.get("kind", "")),
            "title": str(row.get("title", "")),
            "body": str(row.get("body", "")),
            "actor": str(row.get("actor") or ""),
            "target": target if isinstance(target, dict) else None,
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "read": bool(row.get("read")),
        }

    @staticmethod
    def counts_for(grants: HostGrants, counts: dict[str, Any]) -> dict[str, Any]:
        by_source = {str(k): int(v) for k, v in (counts.get("by_source") or {}).items()}
        if grants.allows("notifications"):
            return {"unread": int(counts.get("unread") or 0), "by_source": by_source}
        own = by_source.get(grants.extension_id, 0)
        return {"unread": own, "by_source": {grants.extension_id: own} if own else {}}

    def notify(self, extension_id: str, notification: dict[str, Any]) -> None:
        """The same acceptance a notice arriving over MCP gets."""
        if self.notices is None:
            raise HostModelError(ErrorCode.FAILED, "this igni keeps no notifications")
        outcome = self.notices.receive(extension_id, notification)
        if outcome.verdict.value == "refused":
            raise HostModelError(ErrorCode.REFUSED, outcome.reason)

    # ── Observations ──────────────────────────────────────────────

    def outbound(self, message: msg.Message) -> None:
        """Every message igni sends its windows."""
        try:
            self.broadcasts.observe(message)
        except Exception:
            logger.exception("host model: observing %s failed", getattr(message, "type", "?"))

    def window_attached(self, window_id: str, *, connection_id: str = "", kind: str = "") -> None:
        self.windows.attached(window_id, connection_id=connection_id, kind=kind)

    def window_wrote(self, window_id: str, key: str, value: str) -> None:
        self.windows.written(window_id, key, value)

    def connection_closed(self, connection_id: str) -> None:
        self.windows.detached(connection_id)

    def extension_changed(self, entry: InstalledExtension | None, extension_id: str) -> None:
        """The registry moved: an extension's state, for it and for whoever may see all."""
        if entry is None:
            return
        change = {"extension_id": extension_id, "state": entry.state.value, "reason": entry.reason}
        self.emit(HostTopic.EXTENSION_STATE_CHANGED, change)
        self.emit("self.state_changed", change, only=extension_id)

    def head_changed(self, head: Head) -> None:
        value = {"commit_sha": head.commit_sha, "branch": head.branch}
        self.emit(
            HostTopic.WORKSPACE_HEAD_CHANGED,
            value,
            content=lambda grants: value if grants.workspace.granted else None,
        )

    def files_changed(self, paths: frozenset[str], cap: int) -> None:
        def visible(grants: HostGrants) -> dict[str, Any] | None:
            if not grants.workspace.granted:
                return None
            seen = sorted(p for p in paths if grants.workspace.permits(p))
            if not seen:
                return None
            return {"paths": seen[:cap], "truncated": len(seen) > cap}

        self.emit(HostTopic.WORKSPACE_FILES_CHANGED, None, content=visible)

    def session_switched(self, session_id: str, window: str | None) -> None:
        self.emit(HostTopic.SESSION_SWITCHED, {"session_id": session_id, "window": window})
        for listener in list(self.on_session_switched):
            with contextlib.suppress(Exception):
                listener(session_id)

    # ── Windows ───────────────────────────────────────────────────

    def _push_client_state(self, window_id: str, key: str, value: str) -> None:
        self.backend.broadcast(
            msg.PushNotification(
                channel="client_state_changed",
                payload={"client_id": window_id, "key": key, "value": value},
            )
        )

    def _window_state_changed(self, window_id: str, key: str, value: str) -> None:
        if key == UiKeys.PAGE:
            self.emit("ui.navigated", {"window": window_id, "pages": self.windows.route(window_id)})
        elif key == UiKeys.SIDEBAR:
            self.emit(
                "ui.sidebar_changed",
                {"window": window_id, "sidebar": self.windows.sidebar(window_id)},
            )
        elif key == UiKeys.SESSION and value:
            self.session_switched(value, window_id)
        elif key.startswith(UiKeys.DRAFT_PREFIX) and key == UiKeys.draft(
            self.windows.session(window_id) or ""
        ):
            self.state.drafts[window_id] = value
            self.emit(
                "ui.composer_changed",
                {"window": window_id, "composer": {"text": value, "attachments": [], "mode": None}},
            )

    def _window_changed(self, what: str, window: dict[str, Any]) -> None:
        self.emit(f"windows.{what}", window)


class _PageSink:
    """An extension's pages, reached through the windows."""

    def __init__(self, model: HostModel, extension_id: str) -> None:
        self._model = model
        self._extension_id = extension_id

    def deliver(self, message: SignalMessage) -> None:
        self._model.backend.broadcast(
            msg.PushNotification(
                channel="host_signal",
                payload={
                    "extension_id": self._extension_id,
                    "signal": message.signal,
                    "value": message.value,
                },
            )
        )


def _plain(value: Any) -> Any:
    """Validated values back to plain JSON for the members."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


__all__ = ["HostModel", "HostState"]
