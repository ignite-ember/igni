"""Intercepts: stopping, or changing, what igni is about to do.

A signal tells the extension what happened. An intercept asks it first:

    from igni_extension import Allow, Modify, Reject
    from igni_extension.host_model import Intercepts, PlanSubmitting

    @ext.intercept(Intercepts.PLAN_SUBMITTING)
    async def grounded(plan: PlanSubmitting) -> Allow | Reject:
        if "src/" not in plan.plan:
            return Reject("Name the files the plan changes.")
        return Allow()

The handler is handed the event's value and answers `Allow()`,
`Reject(reason)`, or — where the event can be changed and the manifest
says `"modify": true` — `Modify(value)`, the value with the fields it may
change changed. Returning `None` allows. igni asks only about the events
in the manifest's `permissions.intercepts`, and gives every answer a
deadline that igni sets; a handler that raises, or is too slow, has not
answered, and the event goes ahead or not as igni's documentation for it
says (`docs/HOST_OBJECT_MODEL.md`, *Intercepting*).

An extension is never asked about its own writes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

logger = logging.getLogger(__name__)


class Intercept[T: BaseModel]:
    """One event an extension can intercept, and the value it is handed."""

    def __init__(self, name: str, value: type[T], modify: tuple[str, ...] = ()) -> None:
        self.name = name
        self.value = value
        #: The fields of the value a `Modify` may change; empty, it can
        #: only be allowed or rejected.
        self.modify = modify

    def parse(self, raw: Any) -> T:
        return self.value.model_validate(raw)

    def __repr__(self) -> str:
        return f"Intercept({self.name!r})"


class Allow(BaseModel):
    """Let it happen as it is."""

    outcome: Literal["allow"] = "allow"


class Reject(BaseModel):
    """Stop it. `reason` reaches the agent or the person, so write it for them."""

    outcome: Literal["reject"] = "reject"
    reason: str

    def __init__(self, reason: str, **data: Any) -> None:
        super().__init__(reason=reason, **data)


class Modify(BaseModel):
    """Let it happen, changed: the whole value, with only the fields it may change changed."""

    outcome: Literal["modify"] = "modify"
    value: Any

    def __init__(self, value: Any, **data: Any) -> None:
        super().__init__(value=value, **data)


type Answer = Allow | Reject | Modify
type InterceptHandler[T] = Callable[[T], Awaitable[Answer | None]]


class InterceptHandlers:
    """One handler per event, and the answers they give."""

    def __init__(self) -> None:
        self._handlers: dict[str, tuple[Intercept[Any], InterceptHandler[Any]]] = {}

    def on[T: BaseModel](
        self, intercept: Intercept[T]
    ) -> Callable[[InterceptHandler[T]], InterceptHandler[T]]:
        def register(handler: InterceptHandler[T]) -> InterceptHandler[T]:
            if intercept.name in self._handlers:
                raise ValueError(
                    f"{intercept.name} is intercepted twice; one handler answers for the extension"
                )
            self._handlers[intercept.name] = (intercept, handler)
            return handler

        return register

    def events(self) -> list[str]:
        return sorted(self._handlers)

    async def answer(self, intercept_id: str, event: str, raw: Any) -> dict[str, Any]:
        """The `answer` request's parameters for one intercept."""
        found = self._handlers.get(event)
        if found is None:
            logger.warning(
                "igni: asked about %s, which nothing here intercepts; allowed. "
                "Add a handler, or take it out of permissions.intercepts",
                event,
            )
            return {"intercept": intercept_id, "outcome": "allow"}
        intercept, handler = found
        try:
            answer = await handler(intercept.parse(raw))
        except Exception as exc:
            logger.exception("igni: the handler of %s failed", event)
            return {"intercept": intercept_id, "outcome": "failed", "reason": str(exc)}
        if answer is None:
            answer = Allow()
        body: dict[str, Any] = {"intercept": intercept_id, "outcome": answer.outcome}
        if isinstance(answer, Reject):
            body["reason"] = answer.reason
        elif isinstance(answer, Modify):
            body["value"] = to_jsonable_python(answer.value)
        return body


__all__ = [
    "Allow",
    "Answer",
    "Intercept",
    "InterceptHandler",
    "InterceptHandlers",
    "Modify",
    "Reject",
]
