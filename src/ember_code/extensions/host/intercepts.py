"""Extensions stopping, or changing, what igni is about to do.

A signal tells an extension what happened. An intercept asks it first:
the host hands the event to every extension that declared it
(``permissions.intercepts``), in the super admin's priority order of
extensions, and waits for each one's answer — allow, reject with a reason, or, where the event
can be changed and the extension may change it, the changed value. A
reject ends the chain; a change is what the next one is handed. Nothing
is decided between interceptors: approving a service covers what its
extension intercepts, and the priority order settles who goes first.

The events are the host schema's (``HostSchema.intercepts`` and every
write, ``HostSchema.write_intercepts``); ``docs/HOST_OBJECT_MODEL.md``,
*Intercepting*, is the promise. igni fires them where they happen —
:meth:`Interceptors.run` from the run controller, the tool hook, the
plan tool and the paths every write takes — and an extension's process
answers over its loopback socket (:mod:`.channel`). igni's core does not
know about extensions: it asks :class:`~ember_code.core.gates.EventGate`,
and the backend installs :meth:`Interceptors.ask` there. A page cannot
intercept: igni would then wait on whatever tab happens to be open.

**The host decides what a silence means**, never the extension: an
interceptor that does not answer within :attr:`Interceptors.TIMEOUT_S`,
whose process is not connected, or that answers in a shape the event
does not take, has failed, and the event's ``fails`` says what follows —
``closed``, the event does not happen; ``open``, it happens as if that
interceptor had allowed it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ember_code.core.gates import GateVerdict
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.host.spec import FailMode, HostSchema, InterceptSpec
from ember_code.extensions.host.types import SchemaTypes
from ember_code.extensions.host.wire import InterceptAnswer, InterceptMessage, Outcome
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState

logger = logging.getLogger(__name__)

#: Who made a write the person made in a window.
PERSON = "person"


class InterceptFailure(Exception):
    """An interceptor that did not give an answer the event can take."""


class Interceptor(BaseModel):
    """One extension's interest in one event."""

    extension_id: str
    #: Whether it may change the event: declared, where the event can be changed.
    modify: bool


class Interceptors:
    """Every extension's interceptors, and the answers they owe."""

    #: How long one interceptor has to answer. igni's to set, never an
    #: extension's: an extension that could raise it could stall igni.
    TIMEOUT_S: ClassVar[float] = 2.0

    _shared: ClassVar[Interceptors | None] = None

    def __init__(self, schema: HostSchema = HOST_SCHEMA, *, timeout: float | None = None) -> None:
        self._schema = schema
        self._types = SchemaTypes(schema)
        self._timeout = self.TIMEOUT_S if timeout is None else timeout
        self._registry: ExtensionRegistry | None = None
        self._order: list[str] = []
        self._senders: dict[str, Callable[[str], None]] = {}
        self._connected: dict[str, asyncio.Event] = {}
        self._waiting: dict[str, tuple[str, asyncio.Future[InterceptAnswer]]] = {}
        self._ids = itertools.count(1)

    @classmethod
    def shared(cls) -> Interceptors:
        """The process's interceptors: one set, whoever fires an event."""
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def reset_shared(cls) -> None:
        """Forget the process's interceptors. For tests."""
        cls._shared = None

    # ── Wiring ────────────────────────────────────────────────────

    def attach(self, registry: ExtensionRegistry) -> None:
        """Read who intercepts what from the installed extensions."""
        self._registry = registry

    def set_order(self, order: list[str]) -> None:
        """The super admin's priority order of extensions: the order interceptors run in."""
        self._order = list(order)

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
        for intercept_id, (owner, future) in list(self._waiting.items()):
            if owner == extension_id and not future.done():
                future.set_exception(InterceptFailure("its process went away"))
                self._waiting.pop(intercept_id, None)

    def answered(self, extension_id: str, answer: InterceptAnswer) -> bool:
        """An answer arrived. ``False`` when it answers nothing this extension was asked."""
        waiting = self._waiting.get(answer.intercept)
        if waiting is None or waiting[0] != extension_id:
            return False
        future = waiting[1]
        if not future.done():
            future.set_result(answer)
        return True

    # ── Who intercepts ────────────────────────────────────────────

    def interceptors(self, event: str) -> list[Interceptor]:
        """Everyone who intercepts ``event``, in the order they run."""
        registry = self._registry
        spec = self._schema.intercept(event)
        if registry is None or spec is None:
            return []
        found: list[Interceptor] = []
        for extension_id in registry.ids():
            entry = registry.get(extension_id)
            if entry is None or entry.state is ExtensionState.DISABLED:
                continue
            grant = next(
                (g for g in entry.manifest.permissions.intercepts if g.event == event), None
            )
            if grant is None:
                continue
            if not HostGrants(entry.manifest).allows(spec.permission):
                logger.warning(
                    "%s intercepts %s without %r in permissions.host; it is not asked",
                    extension_id,
                    event,
                    spec.permission,
                )
                continue
            found.append(
                Interceptor(extension_id=extension_id, modify=grant.modify and bool(spec.modify))
            )
        rank = {extension_id: i for i, extension_id in enumerate(self._order)}
        found.sort(key=lambda i: (rank.get(i.extension_id, len(rank)), i.extension_id))
        return found

    def any_for(self, event: str) -> bool:
        """Whether anybody intercepts ``event`` — a fast no for the common case."""
        registry = self._registry
        if registry is None:
            return False
        for extension_id in registry.ids():
            entry = registry.get(extension_id)
            if entry is not None and any(
                g.event == event for g in entry.manifest.permissions.intercepts
            ):
                return True
        return False

    # ── Asking ────────────────────────────────────────────────────

    async def ask(self, event: str, value: dict[str, Any]) -> GateVerdict:
        """The core's question (:class:`~ember_code.core.gates.EventGate`): nobody's write."""
        if not self.any_for(event):
            return GateVerdict(allowed=True, value=value)
        return await self.run(event, value)

    async def run(
        self, event: str, value: BaseModel | dict[str, Any], *, writer: str | None = None
    ) -> GateVerdict:
        """Ask everyone who intercepts ``event``; the verdict, and the value to go ahead with.

        ``writer`` is who is making a write (:data:`PERSON` or an
        extension's id); an extension is not asked about its own writes.
        """
        current = value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)
        spec = self._schema.intercept(event)
        if spec is None:
            return GateVerdict(allowed=True, value=current)
        for interceptor in self.interceptors(event):
            if interceptor.extension_id == writer:
                continue
            try:
                answer = await self._ask(interceptor.extension_id, event, current)
                current_or_reject = self._apply(spec, interceptor, current, answer)
            except InterceptFailure as exc:
                if spec.fails is FailMode.OPEN:
                    logger.warning(
                        "%s did not answer %s (%s); it goes ahead as if allowed",
                        interceptor.extension_id,
                        event,
                        exc,
                    )
                    continue
                return GateVerdict(
                    allowed=False,
                    value=current,
                    by=interceptor.extension_id,
                    reason=f"Not done: the extension {interceptor.extension_id} checks "
                    f"{event} and could not answer ({exc}).",
                )
            if isinstance(current_or_reject, str):
                return GateVerdict(
                    allowed=False,
                    value=current,
                    by=interceptor.extension_id,
                    reason=current_or_reject,
                )
            current = current_or_reject
        return GateVerdict(allowed=True, value=current)

    async def _ask(self, extension_id: str, event: str, value: dict[str, Any]) -> InterceptAnswer:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        connected = self._connected.setdefault(extension_id, asyncio.Event())
        if extension_id not in self._senders:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(connected.wait(), self._timeout)
        send = self._senders.get(extension_id)
        if send is None:
            raise InterceptFailure("its process is not connected")
        intercept_id = f"i-{next(self._ids)}"
        future: asyncio.Future[InterceptAnswer] = loop.create_future()
        self._waiting[intercept_id] = (extension_id, future)
        try:
            frame = InterceptMessage(
                id=intercept_id, event=event, value=to_jsonable_python(value, fallback=str)
            )
            send(frame.model_dump_json())
            remaining = max(0.0, deadline - loop.time())
            return await asyncio.wait_for(future, remaining)
        except TimeoutError as exc:
            raise InterceptFailure(f"no answer within {self._timeout:g}s") from exc
        finally:
            self._waiting.pop(intercept_id, None)

    def _apply(
        self,
        spec: InterceptSpec,
        interceptor: Interceptor,
        current: dict[str, Any],
        answer: InterceptAnswer,
    ) -> dict[str, Any] | str:
        """The value to go on with, or the reason it was rejected."""
        if answer.outcome is Outcome.ALLOW:
            return current
        if answer.outcome is Outcome.FAILED:
            raise InterceptFailure(f"it failed: {answer.reason or 'no reason given'}")
        if answer.outcome is Outcome.REJECT:
            reason = answer.reason.strip()
            if not reason:
                raise InterceptFailure("it rejected without saying why")
            return f"{reason} (the extension {interceptor.extension_id})"
        if not interceptor.modify:
            logger.warning(
                "%s changed %s, which it may not change; its change is ignored",
                interceptor.extension_id,
                spec.name,
            )
            return current
        if not isinstance(answer.value, dict):
            raise InterceptFailure("its change is not the event's value")
        stray = sorted(
            key
            for key, new in answer.value.items()
            if key not in spec.modify and current.get(key) != new
        )
        if stray:
            raise InterceptFailure(f"it changed {', '.join(stray)}, which cannot be changed")
        changed = dict(current)
        for key in spec.modify:
            if key in answer.value:
                changed[key] = answer.value[key]
        problem = self._types.check(spec.value, changed)
        if problem is not None:
            raise InterceptFailure(f"its change does not fit {spec.value}: {problem}")
        return changed


__all__ = [
    "PERSON",
    "InterceptFailure",
    "Interceptor",
    "Interceptors",
]
