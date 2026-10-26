"""Extensions answering igni's hook events.

igni's hook events (``docs/HOOKS.md``) are where anyone outside the
agent gets a say. A settings file's command or HTTP hook is one way in;
an extension's process is the other: it lists the events in
``permissions.hooks`` and answers them with ``@ext.hook`` in the
``igni-extension`` SDK, over the loopback socket igni hands it
(:mod:`.channel`).

The core never imports this. It asks
:class:`~ember_code.core.hooks.extension_gate.ExtensionHookGate`, and the
backend installs :class:`ExtensionHooks` there. On a deciding event the
host asks every extension that hooks it, in the super admin's priority
order, and waits for each: allow, reject with a reason, or — where the
event has something to change — the changed value. A reject ends the
chain; a change is what the next one is handed. Every other event is
told, and nothing waits. Several extensions hooking one event never
collide: approving a service covers what its extension hooks, and the
priority order settles who goes first.

**The host decides what a silence means**, never the extension: an
extension that does not answer within :attr:`ExtensionHooks.TIMEOUT_S`,
whose process is not running or not connected, or that answers in a
shape the event does not take, has failed, and the event's fail mode
says what follows (:class:`~ember_code.core.hooks.extension_gate.HookFailMode`).
A page cannot answer a hook: igni would then wait on whatever tab
happens to be open.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from ember_code.core.hooks.events import HookEvent
from ember_code.core.hooks.extension_gate import (
    ExtensionHookSpec,
    ExtensionHookVerdict,
    HookFailMode,
)
from ember_code.core.mcp.naming import tool_name_prefix
from ember_code.extensions.faults import ExtensionFaults
from ember_code.extensions.host.wire import HookAnswer, HookMessage, Outcome
from ember_code.extensions.installer import server_name
from ember_code.extensions.principal import Principal
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState

logger = logging.getLogger(__name__)


class HookFailure(Exception):
    """An extension that did not give an answer the event can take.

    ``unanswered`` when its process said nothing — it timed out, crashed,
    went away, or its handler raised — which is what makes an extension
    faulty (:class:`~ember_code.extensions.faults.ExtensionFaults`). An
    answer the event cannot take is the extension's bug, not a fault.
    """

    def __init__(self, message: str, *, unanswered: bool = False) -> None:
        super().__init__(message)
        self.unanswered = unanswered


class ExtensionHooks:
    """Every extension's hooks, and the answers they owe."""

    #: How long one extension has to answer. igni's to set, never an
    #: extension's: an extension that could raise it could stall igni.
    TIMEOUT_S: ClassVar[float] = 2.0

    #: States in which a process that is not connected will not be:
    #: asking would only wait out the deadline.
    _NOT_RUNNING: ClassVar[frozenset[ExtensionState]] = frozenset(
        {ExtensionState.DISABLED, ExtensionState.FAILED}
    )

    _shared: ClassVar[ExtensionHooks | None] = None

    def __init__(
        self, *, timeout: float | None = None, faults: ExtensionFaults | None = None
    ) -> None:
        self._timeout = self.TIMEOUT_S if timeout is None else timeout
        self._faults = faults if faults is not None else ExtensionFaults.shared()
        self._registry: ExtensionRegistry | None = None
        self._priority: list[str] = []
        self._senders: dict[str, Callable[[str], None]] = {}
        self._connected: dict[str, asyncio.Event] = {}
        self._waiting: dict[str, tuple[str, asyncio.Future[HookAnswer]]] = {}
        self._ids = itertools.count(1)
        self._principal: Callable[[], Principal | None] = lambda: None

    @classmethod
    def shared(cls) -> ExtensionHooks:
        """The process's hooks: one set, wherever an event fires."""
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def wire(cls, registry: ExtensionRegistry) -> ExtensionHooks:
        """The process's hooks, reading who hooks what from ``registry``."""
        hooks = cls.shared()
        hooks.attach(registry)
        return hooks

    @classmethod
    def reset_shared(cls) -> None:
        """Forget the process's hooks. For tests."""
        cls._shared = None

    # ── Wiring ────────────────────────────────────────────────────

    def attach(self, registry: ExtensionRegistry) -> None:
        """Read who hooks what from the installed extensions."""
        self._registry = registry
        self._faults.attach(registry)

    def set_priority(self, priority: list[str]) -> None:
        """The super admin's priority order of extensions, highest first: hooks run in it."""
        self._priority = list(priority)

    def set_principal(self, principal: Callable[[], Principal | None]) -> None:
        """Who an event is for: asked as each frame goes out, and sent on it."""
        self._principal = principal

    def _principal_wire(self) -> dict[str, Any] | None:
        principal = self._principal()
        return principal.model_dump(mode="json") if principal is not None else None

    def connect(self, extension_id: str, send: Callable[[str], None]) -> None:
        """An extension's process connected: ``send`` puts a frame on its socket."""
        self._senders[extension_id] = send
        self._connected.setdefault(extension_id, asyncio.Event()).set()

    def disconnect(self, extension_id: str, send: Callable[[str], None] | None = None) -> None:
        """Its process went away. Whatever it owed an answer to has failed."""
        if send is not None and self._senders.get(extension_id) != send:
            return
        self._senders.pop(extension_id, None)
        event = self._connected.get(extension_id)
        if event is not None:
            event.clear()
        for hook_id, (owner, future) in list(self._waiting.items()):
            if owner == extension_id and not future.done():
                future.set_exception(HookFailure("its process went away", unanswered=True))
                self._waiting.pop(hook_id, None)

    def answered(self, extension_id: str, answer: HookAnswer) -> bool:
        """An answer arrived. ``False`` when it answers nothing this extension was asked."""
        waiting = self._waiting.get(answer.hook)
        if waiting is None or waiting[0] != extension_id:
            return False
        future = waiting[1]
        if not future.done():
            future.set_result(answer)
        return True

    # ── Who hooks what ────────────────────────────────────────────

    def hookers(self, event: HookEvent) -> list[str]:
        """Every extension that hooks ``event``, in the order they are asked."""
        registry = self._registry
        if registry is None:
            return []
        found = []
        for extension_id in registry.ids():
            entry = registry.get(extension_id)
            if entry is None or entry.state is ExtensionState.DISABLED:
                continue
            if event in entry.manifest.permissions.hooks:
                found.append(extension_id)
        rank = {extension_id: i for i, extension_id in enumerate(self._priority)}
        found.sort(key=lambda extension_id: (rank.get(extension_id, len(rank)), extension_id))
        return found

    def hooks(self, event: HookEvent) -> bool:
        """Whether any extension hooks ``event`` — a fast no for the common case."""
        return bool(self.hookers(event))

    # ── Telling ───────────────────────────────────────────────────

    def tell(self, event: str, payload: dict[str, Any]) -> None:
        """An event that decides nothing: sent to each extension that hooks it, never awaited."""
        frame = HookMessage(
            event=event,
            value=to_jsonable_python(payload, fallback=str),
            principal=self._principal_wire(),
        )
        text = frame.model_dump_json()
        for extension_id in self.hookers(HookEvent(event)):
            send = self._senders.get(extension_id)
            if send is None:
                logger.debug("%s hooks %s but is not connected; not told", extension_id, event)
                continue
            send(text)

    # ── Its tools ─────────────────────────────────────────────────

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        """The agent called a tool; counted for the extension it is from, if any."""
        extension_id = self._owner(tool_name)
        if extension_id is None:
            return
        if answered:
            self._faults.answered(extension_id)
        else:
            self._faults.failed(extension_id, f"its tool {tool_name}: {detail or 'no answer'}")

    def _owner(self, tool_name: str) -> str | None:
        """The extension a registered tool name is from: ``mcp_ext:<id>_<tool>``."""
        registry = self._registry
        if registry is None:
            return None
        owners = [
            extension_id
            for extension_id in registry.ids()
            if tool_name.startswith(f"{tool_name_prefix(server_name(extension_id))}_")
        ]
        # The longest id: ``acme.a`` and ``acme.a_b`` both prefix ``mcp_ext:acme.a_b_x``.
        return max(owners, key=len) if owners else None

    # ── Asking ────────────────────────────────────────────────────

    async def ask(self, spec: ExtensionHookSpec, value: dict[str, Any]) -> ExtensionHookVerdict:
        """Ask everyone who hooks a deciding event; the verdict, and the value to go ahead with."""
        current = dict(value)
        for extension_id in self.hookers(spec.event):
            if self._faults.faulty(extension_id):
                # Not waited on: it has not answered LIMIT times in a row.
                if spec.fails is HookFailMode.OPEN:
                    continue
                return ExtensionHookVerdict(
                    allowed=False,
                    value=current,
                    by=extension_id,
                    reason=self._faults.not_responding(extension_id),
                )
            try:
                answer = await self._ask(extension_id, spec.event, current)
                current_or_reason = self._apply(spec, extension_id, current, answer)
                self._faults.answered(extension_id)
            except HookFailure as exc:
                if exc.unanswered:
                    self._faults.failed(extension_id, f"its {spec.event.value} hook: {exc}")
                else:
                    self._faults.answered(extension_id)
                if spec.fails is HookFailMode.OPEN:
                    logger.warning(
                        "%s did not answer %s (%s); it goes ahead as if allowed",
                        extension_id,
                        spec.event.value,
                        exc,
                    )
                    continue
                return ExtensionHookVerdict(
                    allowed=False,
                    value=current,
                    by=extension_id,
                    reason=f"Not done: the extension {extension_id} checks "
                    f"{spec.event.value} and could not answer ({exc}).",
                )
            if isinstance(current_or_reason, str):
                return ExtensionHookVerdict(
                    allowed=False, value=current, by=extension_id, reason=current_or_reason
                )
            current = current_or_reason
        return ExtensionHookVerdict(allowed=True, value=current)

    async def ask_one(
        self,
        extension_id: str,
        event: str,
        value: dict[str, Any],
        *,
        timeout: float,
    ) -> HookAnswer:
        """Ask one extension about something that is its own, not one of igni's hook events.

        A service event (:mod:`ember_code.extensions.service_events`) is
        for the extension whose service sent it, so it is asked whatever
        its manifest hooks, with a deadline of the caller's — work it may
        do before answering is the point. Raises :class:`HookFailure`
        when it does not answer.
        """
        return await self._ask(extension_id, event, value, timeout=timeout)

    async def _ask(
        self,
        extension_id: str,
        event: HookEvent | str,
        value: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> HookAnswer:
        registry = self._registry
        entry = registry.get(extension_id) if registry is not None else None
        if (
            extension_id not in self._senders
            and entry is not None
            and entry.state in self._NOT_RUNNING
        ):
            detail = f": {entry.reason}" if entry.reason else ""
            raise HookFailure(f"it is not running{detail}", unanswered=True)
        loop = asyncio.get_running_loop()
        allowed = self._timeout if timeout is None else timeout
        deadline = loop.time() + allowed
        connected = self._connected.setdefault(extension_id, asyncio.Event())
        if extension_id not in self._senders:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(connected.wait(), allowed)
        send = self._senders.get(extension_id)
        if send is None:
            raise HookFailure("its process is not connected", unanswered=True)
        hook_id = f"h-{next(self._ids)}"
        future: asyncio.Future[HookAnswer] = loop.create_future()
        self._waiting[hook_id] = (extension_id, future)
        try:
            frame = HookMessage(
                id=hook_id,
                event=event if isinstance(event, str) else event.value,
                value=to_jsonable_python(value, fallback=str),
                principal=self._principal_wire(),
            )
            send(frame.model_dump_json())
            remaining = max(0.0, deadline - loop.time())
            return await asyncio.wait_for(future, remaining)
        except TimeoutError as exc:
            raise HookFailure(f"no answer within {allowed:g}s", unanswered=True) from exc
        finally:
            self._waiting.pop(hook_id, None)

    @staticmethod
    def _apply(
        spec: ExtensionHookSpec,
        extension_id: str,
        current: dict[str, Any],
        answer: HookAnswer,
    ) -> dict[str, Any] | str:
        """The value to go on with, or the reason it was rejected."""
        if answer.outcome is Outcome.ALLOW:
            return current
        if answer.outcome is Outcome.FAILED:
            raise HookFailure(f"it failed: {answer.reason or 'no reason given'}", unanswered=True)
        if answer.outcome is Outcome.REJECT:
            reason = answer.reason.strip()
            if not reason:
                raise HookFailure("it rejected without saying why")
            return f"{reason} (the extension {extension_id})"
        if not spec.modify:
            raise HookFailure(f"it changed {spec.event.value}, which has nothing to change")
        if not isinstance(answer.value, dict):
            raise HookFailure("its change is not the event's value")
        stray = sorted(
            key
            for key, new in answer.value.items()
            if key not in spec.modify and current.get(key) != new
        )
        if stray:
            raise HookFailure(f"it changed {', '.join(stray)}, which cannot be changed")
        changed = dict(current)
        for key in spec.modify:
            if key in answer.value:
                changed[key] = answer.value[key]
        try:
            checked: BaseModel = spec.value.model_validate(changed)
        except ValidationError as exc:
            raise HookFailure(f"its change does not fit {spec.event.value}: {exc}") from exc
        return checked.model_dump(mode="json")


__all__ = ["ExtensionHooks", "HookFailure"]
