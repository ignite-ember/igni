"""Pydantic models for the JSON-RPC 2.0 wire envelope, the LSP
``initialize`` handshake, and on-disk ``.lsp.json`` server config.

Three tiers of models live here:

1. **JSON-RPC envelope** (``JsonRpcRequest`` / ``JsonRpcNotification``
   / ``JsonRpcResponse`` / ``JsonRpcServerRequest`` / ``JsonRpcError``).
   These carry the wire fields ``jsonrpc``, ``id``, ``method``,
   ``params``, ``result``, ``error`` — all spec-lowercase, so
   ``model_dump()`` produces the correct wire shape *without*
   ``by_alias=True``.

2. **LSP ``initialize`` params** (``ClientCapabilities`` /
   ``InitializeParams``). These carry LSP-spec camelCase fields
   (``processId``, ``rootUri``, ``initializationOptions``) and MUST
   be dumped with ``model_dump(by_alias=True, exclude_none=True)``
   for the outgoing payload to match the LSP wire shape. Future
   field additions to these models need matching ``alias=`` values.

3. **On-disk config** (``LspServerConfig`` / ``LspConfigFile`` /
   ``LspConfigLoadError`` / ``LspConfigLoadResult``). ``LspServerConfig``
   owns its own wire-shape parsing via a
   :meth:`LspServerConfig.from_raw` classmethod (defensive
   isinstance guards, camel/snake dual-key acceptance, env
   stringification, namespace prefix). ``LspConfigFile`` types the
   top-level ``{"lspServers": {...}}`` wire shape. The loader
   returns ``LspConfigLoadResult`` so per-file / per-entry parse
   failures surface as data instead of silent skips.

Inbound messages are parsed via :meth:`parse_inbound_message`, which
inspects the ``id`` / ``method`` keys pre-validation and routes to
the correct model — Pydantic's left-to-right union mode would
mis-route a server-to-client request (id+method) as
``JsonRpcResponse``, so explicit discrimination is required.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# JSON-RPC 2.0 ``params`` accepts an object, an array, or omission
# (``None``). We keep it wide to match the spec; the LSP layer's
# method-specific params are validated by the server, not by us.
# ``Union`` (not ``|``) because ``Mapping[str, Any] | list[Any]``
# fails at runtime on Python <3.12 where generic-alias ``|`` isn't
# supported at module scope.
JsonRpcParams = Union[Mapping[str, Any], list[Any], None]  # noqa: UP007


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object — ``code`` is required, ``data``
    is server-defined and free-form."""

    model_config = ConfigDict(extra="allow")

    code: int
    message: str = ""
    data: Any | None = None


class JsonRpcRequest(BaseModel):
    """Client→server request carrying an ``id``. Field names match
    the wire shape exactly, so no ``by_alias`` is needed on dump.

    Note ``params`` is dumped even when ``None`` (``exclude_none``
    is not the default here) — the LSP ``shutdown`` request and
    ``exit`` notification both pass ``params=None`` and some
    servers require the key to be present as JSON ``null``.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: int
    method: str
    params: JsonRpcParams = None


class JsonRpcNotification(BaseModel):
    """Client→server (or server→client) notification — same shape
    as :class:`JsonRpcRequest` minus the ``id``. No response is
    expected."""

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: JsonRpcParams = None


class JsonRpcResponse(BaseModel):
    """Server→client response carrying the request's ``id``.
    Exactly one of ``result`` / ``error`` is populated per the
    JSON-RPC 2.0 spec, but we accept both fields as optional to
    tolerate quirky servers."""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int
    result: Any | None = None
    error: JsonRpcError | None = None


class JsonRpcServerRequest(BaseModel):
    """Server→client request (has BOTH ``id`` and ``method``, e.g.
    ``window/showMessageRequest``). We declared minimal client
    capabilities in ``initialize`` so we don't currently *handle*
    these — we parse and log-drop them, but keeping the model
    around means future capability upgrades can dispatch cleanly."""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int
    method: str
    params: JsonRpcParams = None


InboundMessage = Union[JsonRpcResponse, JsonRpcServerRequest, JsonRpcNotification]  # noqa: UP007


def parse_inbound_message(raw: dict[str, Any]) -> InboundMessage | None:
    """Route a decoded JSON body to the correct envelope model.

    Discrimination rules (JSON-RPC 2.0):

    * ``id`` present, ``method`` present  → server→client request
    * ``id`` present, ``method`` absent   → response to our request
    * ``id`` absent,  ``method`` present  → notification
    * neither                              → malformed, return ``None``

    Returns ``None`` on any validation failure so the reader can
    log-and-drop without dying — a chatty server sending a
    ``window/logMessage`` with an unexpected shape must not kill
    the reader task.
    """
    if not isinstance(raw, dict):
        return None
    has_id = "id" in raw and raw["id"] is not None
    has_method = isinstance(raw.get("method"), str)
    try:
        if has_id and has_method:
            return JsonRpcServerRequest.model_validate(raw)
        if has_id:
            return JsonRpcResponse.model_validate(raw)
        if has_method:
            return JsonRpcNotification.model_validate(raw)
    except Exception:
        # Pydantic ValidationError, or anything else the model
        # coercion raised. Callers treat ``None`` as "drop it".
        return None
    return None


class LspServerInfo(BaseModel):
    """Public snapshot of a single manager-owned LSP server.

    Produced by :meth:`LspServerManager.server_info` /
    :meth:`LspServerManager.all_server_info` so callers (tools,
    panel UI, tests) don't reach into the manager's private
    ``_configs`` / ``_clients`` / ``_launch_errors`` dicts.
    ``model_dump()`` gives the wire shape returned by
    ``LspTools.lsp_list_servers`` — the four field names are
    load-bearing for the agent-visible JSON."""

    name: str
    languages: list[str]
    running: bool
    last_error: str


class ClientCapabilities(BaseModel):
    """Minimal capability declaration for ``initialize``.

    We don't subscribe to workspace events or diagnostics streams
    yet, so the two required top-level keys are declared empty.
    ``extra="allow"`` because the LSP capability tree is enormous
    and future additions shouldn't require touching this model.
    """

    model_config = ConfigDict(extra="allow")

    workspace: dict[str, Any] = Field(default_factory=dict)
    textDocument: dict[str, Any] = Field(default_factory=dict)


class InitializeParams(BaseModel):
    """Typed shape for the LSP ``initialize`` request params.

    Field names are Python-snake but carry ``alias=`` values for
    the LSP camelCase wire shape — callers MUST dump with
    ``model_dump(by_alias=True, exclude_none=True)`` before
    handing off to the JSON-RPC envelope.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    process_id: int | None = Field(default=None, alias="processId")
    root_uri: str | None = Field(default=None, alias="rootUri")
    capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)
    initialization_options: dict[str, Any] = Field(
        default_factory=dict, alias="initializationOptions"
    )


class LspServerConfig(BaseModel):
    """One LSP server's launch + protocol-init config.

    The required minimum is ``command`` — everything else has a
    workable default. Unknown fields are preserved
    (``extra="allow"``) so future Claude Code LSP-manifest additions
    don't bounce the file.

    Field aliases mirror the LSP-spec camelCase (``rootUri``,
    ``initializationOptions``); ``populate_by_name=True`` means
    Python-natural snake_case keys are also accepted. When both
    the alias and the snake_case key appear in the same entry,
    Pydantic's alias resolution prefers the alias (``rootUri``
    wins over ``root_uri``) — this is the historical behaviour
    and matches the LSP spec's canonical shape.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    # ``languages`` is purely informational — used by the
    # ``lsp_query`` tool's discoverability output and by future
    # convenience wrappers that route by file extension. The LSP
    # protocol itself doesn't care.
    languages: list[str] = Field(default_factory=list)
    # ``rootUri`` — workspace root passed in the ``initialize``
    # request. ``None`` means "use the project_dir at launch
    # time".
    root_uri: str | None = Field(default=None, alias="rootUri")
    # Free-form options passed verbatim in ``initializationOptions``.
    initialization_options: dict[str, Any] = Field(
        default_factory=dict, alias="initializationOptions"
    )
    # Optional env overrides for the spawned process — useful for
    # things like ``PYTHONPATH`` or per-server log levels.
    env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_raw(cls, name: str, entry: Any, namespace: str = "") -> LspServerConfig | None:
        """Coerce one raw ``lspServers`` entry into a typed config.

        ``namespace`` is prepended to ``name`` with a colon when
        set (plugin tier) — keeps server names unique across
        tiers without forcing plugin authors to pre-prefix.

        Returns ``None`` when the entry is not a mapping or when
        ``command`` is missing / blank / not a string. Loaders
        should record such rejections as
        :class:`LspConfigLoadError` (Pattern 3 — expected failures
        as data).
        """
        if not isinstance(entry, Mapping):
            return None
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        # Accept both camelCase (LSP spec) and snake_case keys for
        # ``rootUri`` / ``initializationOptions`` — the LSP spec
        # uses camelCase but Python users default to snake. On
        # collision the alias wins (documented in the class
        # docstring).
        root_uri = entry.get("rootUri", entry.get("root_uri"))
        init_opts = entry.get("initializationOptions", entry.get("initialization_options", {}))
        if not isinstance(init_opts, Mapping):
            init_opts = {}
        env = entry.get("env") or {}
        if not isinstance(env, Mapping):
            env = {}
        full_name = f"{namespace}:{name}" if namespace else name
        return cls(
            name=full_name,
            command=command,
            args=list(entry.get("args") or []),
            languages=list(entry.get("languages") or []),
            root_uri=root_uri if isinstance(root_uri, str) else None,
            initialization_options=dict(init_opts),
            env={str(k): str(v) for k, v in env.items()},
        )


class LspConfigFile(BaseModel):
    """Typed root shape of a ``.lsp.json`` file.

    The file may contain other top-level keys (``extra="allow"``)
    but only ``lspServers`` is consumed today. Each entry inside
    the mapping is kept as raw ``Any`` — per-entry parsing lives
    in :meth:`LspServerConfig.from_raw` so the loader can record
    per-entry failures without failing the whole file.
    """

    model_config = ConfigDict(extra="allow")

    lspServers: dict[str, Any] = Field(default_factory=dict)


class LspConfigLoadError(BaseModel):
    """One expected failure surfaced by the LSP config loader.

    ``entry_name`` is ``None`` for whole-file failures (bad JSON,
    unreadable path) and set to the offending server key for
    per-entry rejections (missing command, non-dict payload).
    ``reason`` is a human-readable string — the panel can render
    it verbatim.
    """

    path: str
    entry_name: str | None = None
    reason: str


class LspConfigLoadResult(BaseModel):
    """Return shape of :meth:`LspConfigLoader.load`.

    Callers that only need the merged config dict can grab
    ``.servers`` (back-compat with the pre-refactor
    ``load_lsp_config`` return type). Callers that want to
    surface parse failures to the panel iterate ``.errors``.
    """

    servers: dict[str, LspServerConfig] = Field(default_factory=dict)
    errors: list[LspConfigLoadError] = Field(default_factory=list)


__all__ = [
    "ClientCapabilities",
    "InboundMessage",
    "InitializeParams",
    "JsonRpcError",
    "JsonRpcNotification",
    "JsonRpcParams",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcServerRequest",
    "LspConfigFile",
    "LspConfigLoadError",
    "LspConfigLoadResult",
    "LspServerConfig",
    "LspServerInfo",
    "parse_inbound_message",
]
