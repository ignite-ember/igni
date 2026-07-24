"""Tool registry — maps Claude Code tool names to Agno toolkit instances.

The registry is a thin coordinator: it holds per-session state
(base directory, permissions, session broadcast, cloud credentials)
and delegates all lookup / canonicalisation / build steps to a
:class:`ToolSpecCatalog`. See :mod:`ember_code.core.tools.tool_spec`
for the catalog and per-tool spec subclasses.

Public back-compat surface preserved:

- ``ToolRegistry(...)`` constructor signature.
- ``resolve(names) -> list[Toolkit]`` returning a plain list.
- ``register(name, factory)`` for runtime-added user tools.
- ``normalize_agno_names(names) -> list[str]`` classmethod raising
  ``ValueError`` on unknown names (used by ``core/agents/ephemeral.py``).
- ``available_tools`` property.
- ``load_custom_tools`` and ``cloud_connected``.

The verbose :class:`ToolResolutionResult` shape is also available via
:meth:`resolve_typed` — callers can migrate incrementally.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from agno.tools import Toolkit

from ember_code.core.config.tool_permissions import ToolPermissions
from ember_code.core.tools.custom_loader import load_custom_tools as _load_custom_tools
from ember_code.core.tools.edit import FileEditNotifier
from ember_code.core.tools.tool_spec import (
    NormalizeResult,
    ToolBuildContext,
    ToolResolutionRequest,
    ToolResolutionResult,
    ToolSpec,
    ToolSpecCatalog,
)
from ember_code.core.tools.visualize import BroadcastFn

logger = logging.getLogger(__name__)


# Module-level catalog. Immutable in practice — runtime-registered
# custom factories live in a per-instance dict on ``ToolRegistry``
# (they don't fit the spec shape; they return raw Toolkit instances).
_CATALOG: ToolSpecCatalog = ToolSpecCatalog.default()


class ToolRegistry:
    """Factory that maps tool names to Agno toolkit instances.

    Uses the same tool names as Claude Code (Read, Write, Edit, Bash, etc.)
    and delegates each factory to a matching :class:`ToolSpec` on the
    module-level catalog.

    Integrates with :class:`ToolPermissions` to:

    - Skip denied tools entirely.
    - Pass ``requires_confirmation_tools`` for "ask" tools.

    The :class:`CodeIndexTools` toolkit lazy-builds the per-project
    index on first call so registration stays cheap.
    """

    def __init__(
        self,
        base_dir: str | None = None,
        permissions: ToolPermissions | None = None,
        cloud_token: str | None = None,
        cloud_server_url: str | None = None,
        broadcast: BroadcastFn | None = None,
        file_edit_notifier: FileEditNotifier | None = None,
    ):
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.permissions = permissions or ToolPermissions(project_dir=self.base_dir)
        self._cloud_token = cloud_token
        self._cloud_server_url = cloud_server_url or "https://api.ignite-ember.sh"
        # Session broadcast — only needed by tools that push structured
        # payloads to attached clients (currently only ``Visualize``).
        # ``None`` in headless / test contexts; those tools then no-op
        # on emit instead of raising.
        self._broadcast = broadcast
        # Shared file-edit notifier fan-in. ``None`` here means the
        # constructed ``EmberEditTools`` will fall back to the
        # module-level ``default_file_edit_notifier`` — same instance
        # the push bridge binds to by default, so the shared-listener
        # invariant holds without explicit wiring at every call site.
        self._file_edit_notifier = file_edit_notifier
        # Custom factories registered at runtime via :meth:`register`.
        # These don't fit the spec-catalog shape (they're raw callables
        # returning ``Toolkit`` instances) so they live on the instance
        # alongside — not inside — the shared catalog.
        self._custom_factories: dict[str, Callable[..., object]] = {}
        # Build context — the frozen bag of state each spec's ``build``
        # may need. Recomputing per-resolve is cheap and keeps mutations
        # (e.g. cloud-token rotation) picked up on the next resolve.

    @property
    def file_edit_notifier(self) -> FileEditNotifier | None:
        """Registry-scoped notifier reference. ``None`` when the
        registry relies on the module-level default — callers wiring
        a bridge should use
        :data:`ember_code.core.tools.edit.default_file_edit_notifier`
        directly in that case."""
        return self._file_edit_notifier

    def _build_context(self) -> ToolBuildContext:
        return ToolBuildContext(
            base_dir=self.base_dir,
            broadcast=self._broadcast,
            cloud_token=self._cloud_token,
            cloud_server_url=self._cloud_server_url,
            file_edit_notifier=self._file_edit_notifier,
        )

    @property
    def available_tools(self) -> list[str]:
        """List all available tool names (registry names + aliases + any
        runtime-registered custom factories).

        Matches the surface of the old ``sorted(_factories.keys())``
        which included both ``Bash`` and its ``BashOutput`` alias.
        """
        names = set(_CATALOG.registry_names_with_aliases)
        names.update(self._custom_factories.keys())
        return sorted(names)

    def register(self, name: str, factory: Callable[..., object]) -> None:
        """Register a custom tool factory.

        Custom factories are per-instance and don't fit the shared
        spec-catalog shape (they receive no build context and return a
        pre-configured toolkit). Used by :meth:`load_custom_tools` and
        by third-party integrations that want to inject a bespoke
        toolkit without declaring a full :class:`ToolSpec`.
        """
        self._custom_factories[name] = factory

    def resolve(self, tool_names: list[str] | str) -> list:
        """Resolve tool names to Agno toolkit instances.

        Denied tools are skipped. Tools with ``ask`` permission get
        ``requires_confirmation_tools`` set so Agno triggers HITL.

        Args:
            tool_names: Comma-separated string or list of tool names.

        Returns:
            List of Agno toolkit instances.

        Raises:
            ValueError: if any name is unknown (not in catalog, not a
                custom factory, not an ``MCP:*`` name, not a reserved
                pseudo-tool). Preserved for back-compat with existing
                callers that expect a raise-on-unknown contract.
        """
        request = ToolResolutionRequest(tool_names=tool_names)
        result = self.resolve_typed(request)
        if not result.ok:
            raise ValueError(
                f"Unknown tool: '{result.unknown[0]}'. Available: {self.available_tools}"
            )
        return result.tools

    def resolve_typed(self, request: ToolResolutionRequest) -> ToolResolutionResult:
        """Verbose result-shape resolve — new callers should prefer this.

        Returns a :class:`ToolResolutionResult` with the built toolkits
        alongside diagnostic lists of skipped-denied and unknown names.
        Never raises for unknown names.
        """
        context = self._build_context()
        tools: list[object] = []
        skipped: list[str] = []
        unknown: list[str] = []
        seen: set[str] = set()

        for name in request.tool_names:
            # MCP tools connect after the registry is built; pass through.
            if name.startswith("MCP:"):
                continue
            # Pseudo-tools handled by the agent builder, not here.
            if name in ToolSpecCatalog.RESERVED_NON_TOOL_NAMES:
                continue

            if self.permissions.is_denied(name):
                logger.info("Tool '%s' is denied by permissions — skipping", name)
                skipped.append(name)
                continue

            # Custom runtime-registered factories first — a user calling
            # ``register("Read", ...)`` intends to override the built-in
            # spec of the same name (pre-refactor ``_factories`` was a
            # single dict where ``register`` overwrote built-ins).
            factory = self._custom_factories.get(name)
            if factory is not None:
                if name in seen:
                    continue
                seen.add(name)
                needs_confirm = self.permissions.needs_confirmation(name)
                tools.append(factory(confirm=needs_confirm))
                continue

            # Spec catalog (built-ins) — canonicalises aliases.
            spec = _CATALOG.by_name.get(name)
            if spec is not None:
                if spec.name in seen:
                    continue
                seen.add(spec.name)
                needs_confirm = self.permissions.needs_confirmation(name)
                tools.append(spec.build(context, confirm=needs_confirm))
                continue

            unknown.append(name)

        return ToolResolutionResult(tools=tools, skipped_denied=skipped, unknown=unknown)

    # ── Back-compat classmethod shim ─────────────────────────────
    # ``core/agents/ephemeral.py`` calls
    # ``ToolRegistry.normalize_agno_names(list)`` expecting a plain
    # list back and a ``ValueError`` on any unknown. Forward to the
    # catalog's Result-shape and re-raise the legacy error.

    @classmethod
    def normalize_agno_names(cls, names: list[str]) -> list[str]:
        """Map Agno function names → registry names, deduplicate, and
        validate the result.

        Raises ``ValueError`` if any name maps to an unknown registry
        tool (excluding ``MCP:*`` — those come from server connections
        and aren't in :attr:`ToolSpecCatalog.valid_ephemeral_names`).

        Prefer :meth:`ToolSpecCatalog.normalize` in new code — it
        returns a :class:`NormalizeResult` without raising.
        """
        result: NormalizeResult = _CATALOG.normalize(names)
        if not result.ok:
            raise ValueError(result.as_error_message())
        return result.names

    def load_custom_tools(
        self,
        project_dir: Path | None = None,
        *,
        plugin_tool_dirs: list[tuple[str, Path]] | None = None,
    ) -> list[Toolkit]:
        """Discover custom tools from ``.ember/tools/`` and return as
        toolkit list.

        Scans directories in priority order:

        1. ``~/.ember/tools/`` (global user tools)
        2. ``<project>/.ember/tools/`` (project tools)
        3. Plugin tools (``plugin_tool_dirs``, namespaced
           ``custom_<plugin>_<file>``)
        """
        return _load_custom_tools(
            project_dir or self.base_dir,
            plugin_tool_dirs=plugin_tool_dirs,
        )

    @property
    def cloud_connected(self) -> bool:
        """Whether Ember Cloud tools are available."""
        return self._cloud_token is not None


# Re-export the spec / catalog / result types at the registry module
# so external callers with ``from ember_code.core.tools.registry import
# ToolRegistry`` can access the new shapes without extra imports.
__all__ = [
    "NormalizeResult",
    "ToolBuildContext",
    "ToolRegistry",
    "ToolResolutionRequest",
    "ToolResolutionResult",
    "ToolSpec",
    "ToolSpecCatalog",
]
