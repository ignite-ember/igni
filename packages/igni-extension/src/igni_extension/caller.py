"""Who a call is for: `Caller`.

Every request igni sends this process — a tool call, a page's call, a
hook — names the producer it is for: a person (with their organisation
and the one group they belong to) or an app. A tool or hook handler that wants it declares a
parameter typed `Caller`, and the SDK passes it in; anything running
inside that call can also ask `Caller.current()`.

    @ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
    async def mine(caller: Caller) -> list[str]:
        response = await ext.service.for_(caller).get("/v1/things")
        ...

A tool typed `caller: Caller` fails when igni sent no caller; one typed
`caller: Caller | None` runs with `None` instead.

The extension never decides who may call it: igni only forwards a call
the producer may make. Its job is to serve the caller it is handed — and
to keep callers apart, which is why the caller lives in the request, not
in a global. Concurrent calls each see their own (`contextvars`), and a
plain `def` running in a worker thread sees its call's caller too.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from enum import StrEnum
from functools import wraps
from typing import Any, ClassVar, get_type_hints

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from pydantic import BaseModel, ConfigDict, ValidationError


class CallerKind(StrEnum):
    PERSON = "person"
    APP = "app"
    #: One end user of an app, a producer of its own.
    SPACE = "space"


class NoCaller(LookupError):
    """Asked for the caller outside a call igni made: in `on_start` work, say."""


class Caller(BaseModel):
    """The producer one call is for."""

    model_config = ConfigDict(frozen=True)

    #: Where the host puts it: the MCP request's `_meta` key, and the hook frame's field.
    META_KEY: ClassVar[str] = "igni/principal"
    #: How a token request names whom the token is for.
    TOKEN_HEADER: ClassVar[str] = "X-Igni-Principal"  # noqa: S105 — a header's name, not a secret

    kind: CallerKind
    #: The person's or the app's id on igni server.
    id: str
    #: Their organisation.
    org: str | None = None
    #: The one group a person belongs to in `org`; an app has none.
    group: str | None = None
    #: The igni session the call comes from, when there is one.
    session: str | None = None
    #: A space's app (its id); `None` for a person or an app.
    app: str | None = None
    #: The app's own name for a space's end user.
    external_id: str | None = None

    @property
    def ref(self) -> str:
        """`person:<id>` — how the host's token endpoint is told whom a token is for."""
        return f"{self.kind.value}:{self.id}"

    @classmethod
    def of(cls, raw: Any) -> Caller | None:
        """A caller from the wire, or `None` when there is none or it is malformed."""
        if not isinstance(raw, dict):
            return None
        try:
            return cls.model_validate(raw)
        except ValidationError:
            return None

    @classmethod
    def current(cls) -> Caller | None:
        """The caller of the call being served now, or `None` outside one."""
        scoped = _SCOPE.get()
        if scoped is not None:
            return scoped
        meta = RequestMeta.current()
        return cls.of(meta.get(cls.META_KEY)) if meta is not None else None

    @classmethod
    def required(cls) -> Caller:
        """The current caller; `NoCaller` outside a call igni made."""
        caller = cls.current()
        if caller is None:
            raise NoCaller(
                "no caller: this runs outside a call igni made, or igni sent none. "
                "Work with no caller uses the service as the extension itself."
            )
        return caller


#: The caller of the call being served, where the SDK set it itself (a hook).
_SCOPE: ContextVar[Caller | None] = ContextVar("igni_caller", default=None)

#: The `_meta` of the MCP request being served, set by `RequestMeta.middleware`.
_META: ContextVar[dict[str, Any] | None] = ContextVar("igni_request_meta", default=None)


class RequestMeta:
    """The `_meta` of the MCP request this code is serving, wherever it runs.

    Set for the length of each request by `middleware`, which the
    extension's server runs on every message igni sends, so a tool — and
    anything it calls, on the loop or in a worker thread — reads the
    request it serves: concurrent calls each see their own.
    """

    @staticmethod
    def current() -> dict[str, Any] | None:
        """The current request's `_meta`, or `None` outside a request (or when it had none)."""
        return _META.get()

    @staticmethod
    async def middleware(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        """An MCP server middleware: the request's `_meta` is current while it is served."""
        token = _META.set(dict(ctx.meta) if ctx.meta else None)
        try:
            return await call_next(ctx)
        finally:
            _META.reset(token)


class CallerScope:
    """Sets the caller for the call being served, and finds where a function wants it."""

    @staticmethod
    @contextlib.contextmanager
    def of(caller: Caller | None) -> Iterator[None]:
        token = _SCOPE.set(caller)
        try:
            yield
        finally:
            _SCOPE.reset(token)

    @staticmethod
    def parameter(fn: Callable[..., Any]) -> str | None:
        """The name of `fn`'s parameter typed `Caller` (or `Caller | None`), if it has one."""
        try:
            hints = get_type_hints(fn)
        except (NameError, TypeError):
            hints = getattr(fn, "__annotations__", {})
        for name, annotation in hints.items():
            if name != "return" and annotation in (Caller, Caller | None):
                return name
        return None

    @staticmethod
    def optional(fn: Callable[..., Any], name: str) -> bool:
        """Whether `fn` takes `Caller | None` there: it runs without one, rather than failing."""
        try:
            return bool(get_type_hints(fn).get(name) == Caller | None)
        except (NameError, TypeError):
            return False

    @classmethod
    def inject(cls, fn: Callable[..., Any]) -> Callable[..., Any]:
        """`fn` with its `Caller` parameter filled from the call, and hidden from its schema.

        The parameter is not something igni or a model supplies, so it
        is not part of the tool's input: the wrapper's signature leaves
        it out, and the schema derived from it does too.
        """
        name = cls.parameter(fn)
        if name is None:
            return fn
        find: Callable[[], Caller | None] = (
            Caller.current if cls.optional(fn, name) else Caller.required
        )
        signature = inspect.signature(fn)
        # Resolved now, against the author's module: the wrapper lives in
        # this one, and a string annotation evaluated here would not find
        # the author's types.
        try:
            resolved = get_type_hints(fn)
        except (NameError, TypeError):
            resolved = dict(getattr(fn, "__annotations__", {}))
        visible = [
            p.replace(annotation=resolved.get(p.name, p.annotation))
            for p in signature.parameters.values()
            if p.name != name
        ]
        hints = {k: v for k, v in resolved.items() if k != name}
        return_annotation = resolved.get("return", signature.return_annotation)

        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def with_caller_async(*args: Any, **kwargs: Any) -> Any:
                return await fn(*args, **kwargs, **{name: find()})

            wrapper: Callable[..., Any] = with_caller_async
        else:

            @wraps(fn)
            def with_caller(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, **kwargs, **{name: find()})

            wrapper = with_caller
        wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=visible, return_annotation=return_annotation
        )
        wrapper.__annotations__ = hints
        # `wraps` points `__wrapped__` at `fn`, and `inspect.signature`
        # follows it back to the hidden parameter.
        del wrapper.__wrapped__  # type: ignore[attr-defined]
        return wrapper


__all__ = ["Caller", "CallerKind", "CallerScope", "NoCaller", "RequestMeta"]
