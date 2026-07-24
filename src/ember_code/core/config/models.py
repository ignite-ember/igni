"""Model registry — maps model names to Agno model instances.

This module now hosts ONLY the registry itself. Everything else it
used to accrete (import-time logger side effect, null-object
placeholder, message sanitizer, stack-walking helper, streaming
logging wrapper, dispatch-branching provider builders) has moved to
its own single-concern module. The old private names remain
importable through the re-export block at the bottom so tests and
external callers keep working — new code should reach for the real
homes listed in each import comment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agno.models.base import Model
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ember_code.cli.options import CliOptions

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.context_window import (
    DEFAULT_CONTEXT_WINDOW,
    ContextWindowResolver,
)
from ember_code.core.config.llm_call_logger import LlmCallLogger
from ember_code.core.config.logging_model import LoggingModel
from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.config.null_model import NoModelConfigured
from ember_code.core.config.permission_eval import PermissionMode
from ember_code.core.config.provider_builders import ProviderClientBuilder
from ember_code.core.config.provider_catalog import ProviderCatalog
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


# ── Typed CLI override models ────────────────────────────────────
#
# These bundles replace the raw-dict scaffolding the CLI callback
# used to build by hand. ``CliOverrides.from_options(opts)`` is the
# single owner of the "strictest-wins" precedence rule for the
# permission flags — every branch that used to ``setdefault(...)``
# a nested dict now runs through one classmethod that reads a typed
# :class:`CliOptions` bundle and returns a validated shape.
#
# ``to_settings_payload()`` is the seam back to the existing
# :meth:`SettingsLoader.merge_cli` API, which still expects a
# dict[str, Any]. Nothing else in the codebase needs to change —
# the dict shape emitted here is byte-for-byte the shape the old
# procedural code produced.


class PermissionOverrides(BaseModel):
    """CLI-supplied permission overrides.

    ``mode`` is typed via :class:`PermissionMode` (the same enum the
    :class:`PermissionEvaluator` reads) so the strictest-wins policy
    is enforced by the type layer rather than free-form strings.
    The legacy per-category ``file_write`` / ``shell_execute`` / ...
    fields are kept for third-party code that reads
    ``Settings.permissions.file_write`` etc.; new code should look
    at ``mode`` instead. Marked TODO(v0.10) for removal — the
    ``PermissionGuard`` that used them is dead code today.
    """

    mode: PermissionMode | None = None
    file_write: str | None = None
    shell_execute: str | None = None
    git_push: str | None = None
    git_destructive: str | None = None
    web_search: str | None = None
    web_fetch: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Dict shape :meth:`SettingsLoader.merge_cli` still expects.
        Only fields that were actually set are emitted, so downstream
        deep-merge doesn't clobber unrelated permissions with ``None``.
        """
        payload: dict[str, Any] = {}
        if self.mode is not None:
            payload["mode"] = self.mode.value
        for name in (
            "file_write",
            "shell_execute",
            "git_push",
            "git_destructive",
            "web_search",
            "web_fetch",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


class CliOverrides(BaseModel):
    """Typed bundle of CLI-driven ``Settings`` overrides.

    Constructed via :meth:`from_options` — that classmethod owns the
    permissive-to-strict precedence order that used to live inline
    in the click callback. ``to_settings_payload`` emits the
    dict shape :meth:`SettingsLoader.merge_cli` still consumes so
    the loader itself doesn't need to change.
    """

    model_default: str | None = None
    show_routing: bool | None = None
    show_reasoning: bool | None = None
    show_tool_calls: bool | None = None
    permissions: PermissionOverrides = Field(default_factory=PermissionOverrides)

    @classmethod
    def from_options(cls, opts: CliOptions) -> CliOverrides:
        """Build overrides from a :class:`CliOptions` bundle.

        Order is "permissive → strict" so the strictest passed flag
        wins via standard field overwrite: ``--auto-approve --strict``
        ends up in ``dontAsk``. Matches the "safety beats convenience"
        principle used elsewhere in the permission stack.
        """
        overrides = cls()
        if opts.model:
            overrides.model_default = opts.model
        if opts.verbose:
            overrides.show_routing = True
            overrides.show_reasoning = True
        if opts.quiet:
            overrides.show_tool_calls = False
            overrides.show_routing = False

        perms = overrides.permissions
        if opts.auto_approve:
            perms.mode = PermissionMode.BYPASS_PERMISSIONS
            perms.file_write = "allow"
            perms.shell_execute = "allow"
            perms.git_push = "allow"
            perms.git_destructive = "allow"
        if opts.accept_edits:
            perms.mode = PermissionMode.ACCEPT_EDITS
            perms.file_write = "allow"
        if opts.read_only:
            perms.mode = PermissionMode.PLAN
            perms.file_write = "deny"
            perms.shell_execute = "deny"
        if opts.strict:
            perms.mode = PermissionMode.DONT_ASK
            perms.file_write = "deny"
            perms.shell_execute = "deny"
            perms.git_push = "deny"
            perms.git_destructive = "deny"
        if opts.no_web:
            perms.web_search = "deny"
            perms.web_fetch = "deny"

        return overrides

    def is_empty(self) -> bool:
        """True when no override was actually populated. The
        :func:`load_settings` seam accepts ``None`` for
        "no overrides", which lets loader-side tests distinguish
        the "user passed no flags" path from "user passed flags
        that resolved to defaults"."""
        if (
            self.model_default is not None
            or self.show_routing is not None
            or self.show_reasoning is not None
            or self.show_tool_calls is not None
        ):
            return False
        return not self.permissions.to_payload()

    def to_settings_payload(self) -> dict[str, Any] | None:
        """Emit the dict shape :meth:`SettingsLoader.merge_cli`
        consumes. Returns ``None`` when no override is populated so
        the loader can distinguish "user passed nothing" from "user
        passed something that happens to match defaults"."""
        if self.is_empty():
            return None
        payload: dict[str, Any] = {}
        if self.model_default is not None:
            payload.setdefault("models", {})["default"] = self.model_default
        display: dict[str, Any] = {}
        if self.show_routing is not None:
            display["show_routing"] = self.show_routing
        if self.show_reasoning is not None:
            display["show_reasoning"] = self.show_reasoning
        if self.show_tool_calls is not None:
            display["show_tool_calls"] = self.show_tool_calls
        if display:
            payload["display"] = display
        perms_payload = self.permissions.to_payload()
        if perms_payload:
            payload["permissions"] = perms_payload
        return payload

    def as_merge_dict(self) -> dict[str, Any]:
        """Return the merge-ready dict form for
        :meth:`SettingsLoader.merge_cli`.

        Thin sibling of :meth:`to_settings_payload` — the difference
        is the return type. ``to_settings_payload`` returns
        ``None`` for the empty case (used by the CLI seam that
        wants to distinguish "no flags" from "flags matching
        defaults"); ``as_merge_dict`` returns ``{}`` so the loader
        can pass the result straight into ``deep_merge`` without a
        None-guard.
        """
        return self.to_settings_payload() or {}


class ModelRegistry:
    """Registry that maps model names to Agno model instances.

    All models (including Ember defaults) are defined in the config
    registry (``models.registry``). Built-in defaults ship via the
    ``Settings`` Pydantic model (call ``Settings.defaults()``) and
    can be overridden by user/project config files.

    Resolution order:
    1. Config registry (defaults + user overrides).
    2. ``provider:model_id`` format (e.g., ``openai_like:gpt-4o``).

    Instance collaborators:
    * :class:`ProviderCatalog` — polymorphic dispatch to builders.
    * :class:`LlmCallLogger` — dedicated LLM-call log handler.
    * :class:`ContextWindowResolver` — model-size resolution.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._catalog = ProviderCatalog()
        self._llm_logger = LlmCallLogger()

        # Resolve cloud credentials for inference routing. Cached
        # per-registry-instance rather than per-lookup so a single
        # session doesn't re-read the credentials file for every
        # ``get_model`` call.
        self._credentials = CloudCredentials(settings.auth.credentials_file)
        self._cloud_token = self._credentials.access_token
        self._cloud_server_url = settings.api_url if self._cloud_token else None

        # Context-window resolver reuses the registry's already-derived
        # cloud token so ``/models/{id}`` fetches against the Ember
        # gateway don't re-read credentials from disk.
        self.context_windows = ContextWindowResolver(cloud_token=self._cloud_token)

    # ── Public: model resolution ──────────────────────────────────

    def get_model(self, name: str | None = None) -> Model:
        """Get an Agno model instance by registry name.

        Returns whatever concrete :class:`agno.models.base.Model`
        subclass the resolved provider produces —
        :class:`~ember_code.core.config.logging_model.LoggingModel`
        (an ``OpenAILike`` subclass) for OpenAI-compatible providers,
        :class:`agno.models.google.Gemini` for the Gemini path.

        When no model resolves (registry empty AND no default —
        e.g. brand-new install before ``/login``, or a stale token
        that returned no entries from cloud discovery) we return a
        :class:`NoModelConfigured` placeholder so the session can
        still construct. Real invocation raises a clear error; the
        TUI stays reachable so the user can run ``/login``.

        Raises :class:`ValueError` for user-configuration errors
        (unknown model name, unknown provider).
        """
        resolved_name = name if name else self._effective_default()
        if not resolved_name:
            self._warn_no_model()
            return NoModelConfigured.for_login_required()

        entry = self._resolve_entry(resolved_name)
        if entry is None:
            raise ValueError(
                f"Unknown model {resolved_name!r}. "
                "Add an entry to `models.registry` or use a "
                "`provider:model_id` name."
            )

        builder = self._catalog.builder_for(entry.provider)
        if builder is None:
            available = ", ".join(sorted(self._catalog.available_providers())) or "(none)"
            raise ValueError(f"Unknown provider {entry.provider!r}. Available: {available}.")

        return builder.build(
            entry,
            cloud_token=self._cloud_token,
            llm_logger=self._llm_logger,
        )

    def get_context_window(self, name: str | None = None) -> int:
        """Get the context window size for a model.

        Bootstrap-safe: when no model is configured (registry empty,
        no default) we return the configured ``max_context_window``
        instead of raising. The session must be able to construct
        even when cloud discovery hasn't populated the registry yet
        — otherwise the user can't reach ``/login`` to fix it.
        """
        resolved_name = name if name else self._effective_default()
        if not resolved_name:
            return self.settings.models.max_context_window
        entry = self._resolve_entry(resolved_name)
        model_id = entry.model_id if entry else resolved_name
        return self.context_windows.resolve(model_id, entry)

    async def aget_context_window(self, name: str | None = None) -> int:
        """Get the context window size, with async API fallback."""
        resolved_name = name if name else self._effective_default()
        if not resolved_name:
            return self.settings.models.max_context_window
        entry = self._resolve_entry(resolved_name)
        model_id = entry.model_id if entry else resolved_name
        return await self.context_windows.aresolve(model_id, entry)

    # ── Public: catalog / provider registration ───────────────────

    def register_provider(self, name: str, builder: ProviderClientBuilder) -> None:
        """Register a custom provider builder for this registry
        instance. Kept for parity with the old classvar API — now
        delegates to the per-instance catalog so tests get a fresh
        surface per registry rather than mutating a shared singleton.
        """
        self._catalog.register(name, builder)

    # ── Internals ─────────────────────────────────────────────────

    def _warn_no_model(self) -> None:
        logger.warning(
            "No model configured — returning placeholder. "
            "Run /login or add a model to models.registry."
        )

    def _effective_default(self) -> str:
        """Return the active default model name, or ``""`` when none.

        Resolution order:

        1. ``settings.models.default`` if explicitly set (user override,
           ``/model`` switch, or cloud-discovery auto-assign).
        2. First key in ``settings.models.registry`` — works as soon
           as cloud discovery has merged at least one entry.
        3. ``""`` otherwise — bootstrap-safe. :meth:`get_model` maps
           the empty case to :class:`NoModelConfigured` so the session
           can still construct and the user can reach ``/login`` to
           fix the underlying problem (no cloud token, org-membership
           403, network down, etc.).
        """
        explicit = self.settings.models.default
        if explicit:
            return explicit
        if self.settings.models.registry:
            return next(iter(self.settings.models.registry))
        return ""

    def _resolve_entry(self, name: str) -> ModelRegistryEntry | None:
        """Resolve a model name to a typed registry entry.

        ``settings.models.registry`` stores a mix of shapes:
        cloud-discovery now writes typed :class:`ModelRegistryEntry`
        instances directly (see
        :meth:`CloudModelCatalogClient.merge_into`), but user-authored
        YAML entries still arrive as raw ``dict`` from Pydantic's
        YAML load path, and tests occasionally re-assign raw dicts.
        We coerce here at lookup time so every downstream reader
        sees a real :class:`ModelRegistryEntry` regardless of which
        source produced the row.
        """
        raw = self.settings.models.registry.get(name)
        if raw is not None:
            return self._coerce_entry(raw)
        if ":" in name:
            provider, model_id = name.split(":", 1)
            return ModelRegistryEntry(provider=provider, model_id=model_id)
        return None

    @staticmethod
    def _coerce_entry(raw: ModelRegistryEntry | dict) -> ModelRegistryEntry:
        if isinstance(raw, ModelRegistryEntry):
            return raw
        return ModelRegistryEntry.model_validate(raw)


# ── Backward-compat re-exports ─────────────────────────────────────
#
# The pre-refactor module exposed a handful of underscore-prefixed
# symbols that tests and adjacent modules pull from
# ``ember_code.core.config.models``. Rather than force every caller
# to update its import path in the same diff, we alias the new
# public names back to their old private ones here. The real homes
# are documented on the alias RHS.

_LoggingModel = LoggingModel  # real home: logging_model.py
_NoModelConfigured = NoModelConfigured  # real home: null_model.py

# Tool-arg streaming primitives — real home: model_stream.py. The
# streaming test suite imports them via this module.
from ember_code.core.config.model_stream import (  # noqa: E402
    _aemit_tool_arg_deltas,
    _emit_tool_arg_delta_events,
    _emit_tool_arg_deltas,
    _ToolCallAccumulator,
    _ToolCallAccumulatorStore,
    _ToolCallFragment,
)

__all__ = [
    "CliOverrides",
    "DEFAULT_CONTEXT_WINDOW",
    "ContextWindowResolver",
    "LoggingModel",
    "ModelRegistry",
    "ModelRegistryEntry",
    "NoModelConfigured",
    "PermissionMode",
    "PermissionOverrides",
    "ProviderClientBuilder",
    # Backward-compat private aliases.
    "_LoggingModel",
    "_NoModelConfigured",
    "_ToolCallAccumulator",
    "_ToolCallAccumulatorStore",
    "_ToolCallFragment",
    "_aemit_tool_arg_deltas",
    "_emit_tool_arg_delta_events",
    "_emit_tool_arg_deltas",
]
