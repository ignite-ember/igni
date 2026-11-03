"""Tool names as each provider is sent them, and back.

igni registers a tool from an MCP server as ``mcp_<server>_<tool>``, and an
extension's server is ``ext:<id>`` — so an extension's tool is
``mcp_ext:acme.tickets_lookup_ticket``. That name is load-bearing inside
igni (permissions, hints, hooks, ``--allowedTools``, transcripts all key on
it) and is refused by every hosted provider: OpenAI's function names are
``^[a-zA-Z0-9_-]{1,64}$``, Anthropic's the same (128 on some models),
Gemini's must also start with a letter or an underscore. A ``:`` or a
``.`` is a 400, so no extension tool could be offered to a hosted model.

So the name is rewritten **at the provider boundary and nowhere else**:
:class:`ToolNameBoundary` sits in each model class igni builds
(``LoggingModel`` for ``openai_like``, ``LoggingClaude`` for ``anthropic``,
``CheckedGemini`` for ``gemini``), rewrites the tool definitions and the
names in the conversation history on the way out, and maps every tool
call on the way back before Agno looks its function up. Nothing above
the model class ever sees a wire name.

The rule, which is the same for every provider so a conversation can move
between them:

* a name that is already valid (``^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$`` — every
  built-in, every ``.mcp.json`` server with a plain name) is sent as it is;
* otherwise every other character becomes ``_`` (``mcp_ext:acme.tickets_lookup_ticket``
  → ``mcp_ext_acme_tickets_lookup_ticket``), a leading digit or ``-`` gets an
  ``_`` in front, and
* if that is longer than 64 or is already another tool's name in the same
  request, it is cut to 55 characters and ``_`` plus the first eight hex
  digits of the original name's SHA-256 is added — stable across requests
  and processes.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterable
from typing import Any

#: OpenAI's charset and length, with Gemini's rule for the first character.
VALID_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$")
MAX_LENGTH = 64
_INVALID = re.compile(r"[^a-zA-Z0-9_-]")


def wire_name(name: str) -> str:
    """``name`` as a provider accepts it, before collisions are considered."""
    if VALID_NAME.match(name):
        return name
    candidate = _INVALID.sub("_", name)
    if not candidate or not (candidate[0].isalpha() or candidate[0] == "_"):
        candidate = f"_{candidate}"
    if len(candidate) > MAX_LENGTH:
        return _hashed(name, candidate)
    return candidate


def _hashed(name: str, candidate: str) -> str:
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{candidate[: MAX_LENGTH - 9]}_{digest}"


class ProviderToolNames:
    """The names one model has sent, and what each stands for.

    Kept per model instance and only ever added to, so a reply is mapped
    back by the same table that named its request.
    """

    def __init__(self) -> None:
        self._back: dict[str, str] = {}

    def assign(self, names: Iterable[str]) -> dict[str, str]:
        """Each of ``names`` → the name a provider is sent, collision-free."""
        unique = sorted(set(names))
        mapping: dict[str, str] = {n: n for n in unique if VALID_NAME.match(n)}
        taken = set(mapping)
        for name in unique:
            if name in mapping:
                continue
            wire = wire_name(name)
            if wire in taken:
                wire = _hashed(name, _INVALID.sub("_", name))
            taken.add(wire)
            mapping[name] = wire
        for name, wire in mapping.items():
            if wire != name:
                self._back[wire] = name
        return mapping

    def registered(self, wire: str) -> str:
        """The registered name a provider's ``wire`` name stands for."""
        return self._back.get(wire, wire)


class ToolNameBoundary:
    """Mixed into igni's model classes, before Agno's provider class.

    Rewrites ``tools``, ``tool_choice`` and the tool names in ``messages``
    on every ``invoke``/``ainvoke``/``invoke_stream``/``ainvoke_stream``,
    and maps tool calls back in ``_parse_provider_response``,
    ``_parse_provider_response_delta`` and ``parse_tool_calls``.
    """

    def _tool_names(self) -> ProviderToolNames:
        names = self.__dict__.get("_igni_tool_names")
        if names is None:
            names = ProviderToolNames()
            self.__dict__["_igni_tool_names"] = names
        return names

    # ── Out ────────────────────────────────────────────────────────

    def _to_wire(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        messages = kwargs.get("messages", args[0] if args else None)
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")

        names: list[str] = []
        for t in tools or []:
            name = _definition_name(t)
            if name:
                names.append(name)
        for message in messages or []:
            for call in getattr(message, "tool_calls", None) or []:
                name = _call_name(call)
                if name:
                    names.append(name)
            tool_name = getattr(message, "tool_name", None)
            if isinstance(tool_name, str):
                names.append(tool_name)
        if isinstance(tool_choice, dict):
            name = _definition_name(tool_choice)
            if name:
                names.append(name)

        mapping = self._tool_names().assign(names)
        if all(k == v for k, v in mapping.items()):
            return args, kwargs

        kwargs = dict(kwargs)
        if tools is not None:
            kwargs["tools"] = [_rename_definition(t, mapping) for t in tools]
        if isinstance(tool_choice, dict):
            kwargs["tool_choice"] = _rename_definition(tool_choice, mapping)
        if messages is not None:
            renamed = [_rename_message(m, mapping) for m in messages]
            if "messages" in kwargs or not args:
                kwargs["messages"] = renamed
            else:
                args = (renamed, *args[1:])
        return args, kwargs

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._to_wire(args, kwargs)
        return super().invoke(*args, **kwargs)  # type: ignore[misc]

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._to_wire(args, kwargs)
        return await super().ainvoke(*args, **kwargs)  # type: ignore[misc]

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._to_wire(args, kwargs)
        yield from super().invoke_stream(*args, **kwargs)  # type: ignore[misc]

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._to_wire(args, kwargs)
        async for chunk in super().ainvoke_stream(*args, **kwargs):  # type: ignore[misc]
            yield chunk

    # ── Back ───────────────────────────────────────────────────────

    def _from_wire(self, response: Any) -> Any:
        calls = getattr(response, "tool_calls", None)
        if calls:
            names = self._tool_names()
            for call in calls:
                _rename_call_in_place(call, names)
        return response

    def _parse_provider_response(self, *args: Any, **kwargs: Any) -> Any:
        return self._from_wire(super()._parse_provider_response(*args, **kwargs))  # type: ignore[misc]

    def _parse_provider_response_delta(self, *args: Any, **kwargs: Any) -> Any:
        return self._from_wire(super()._parse_provider_response_delta(*args, **kwargs))  # type: ignore[misc]

    def parse_tool_calls(self, tool_calls_data: Any) -> Any:
        parsed = super().parse_tool_calls(tool_calls_data)  # type: ignore[misc]
        names = self._tool_names()
        for call in parsed or []:
            _rename_call_in_place(call, names)
        return parsed


def _definition_name(definition: Any) -> str | None:
    """A tool definition's name: ``{"type": "function", "function": {"name": …}}``."""
    if not isinstance(definition, dict):
        return None
    function = definition.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    name = definition.get("name")
    return name if isinstance(name, str) else None


def _call_name(call: Any) -> str | None:
    if isinstance(call, dict):
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        return name if isinstance(name, str) else None
    function = getattr(call, "function", None)
    name = getattr(function, "name", None) if function is not None else None
    return name if isinstance(name, str) else None


def _rename_definition(definition: Any, mapping: dict[str, str]) -> Any:
    if not isinstance(definition, dict):
        return definition
    function = definition.get("function")
    if isinstance(function, dict) and function.get("name") in mapping:
        return {**definition, "function": {**function, "name": mapping[function["name"]]}}
    if definition.get("name") in mapping:
        return {**definition, "name": mapping[definition["name"]]}
    return definition


def _rename_message(message: Any, mapping: dict[str, str]) -> Any:
    update: dict[str, Any] = {}
    calls = getattr(message, "tool_calls", None)
    if calls:
        renamed = []
        changed = False
        for call in calls:
            name = _call_name(call)
            if isinstance(call, dict) and name in mapping and mapping[name] != name:
                call = {**call, "function": {**call["function"], "name": mapping[name]}}
                changed = True
            renamed.append(call)
        if changed:
            update["tool_calls"] = renamed
    tool_name = getattr(message, "tool_name", None)
    if isinstance(tool_name, str) and mapping.get(tool_name, tool_name) != tool_name:
        update["tool_name"] = mapping[tool_name]
    if not update:
        return message
    return message.model_copy(update=update)


def _rename_call_in_place(call: Any, names: ProviderToolNames) -> None:
    if isinstance(call, dict):
        function = call.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            function["name"] = names.registered(function["name"])
        return
    function = getattr(call, "function", None)
    name = getattr(function, "name", None) if function is not None else None
    if isinstance(name, str):
        # A frozen SDK type keeps its name; the assembled dict is mapped later.
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            function.name = names.registered(name)  # type: ignore[union-attr]


__all__ = ["MAX_LENGTH", "VALID_NAME", "ProviderToolNames", "ToolNameBoundary", "wire_name"]
