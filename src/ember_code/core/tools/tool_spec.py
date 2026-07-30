"""Tool specifications and catalog for the tool registry.

This module holds the Pydantic ``ToolSpec`` base class plus one subclass
per built-in toolkit exposed via :class:`ToolRegistry`. A single
:class:`ToolSpecCatalog` owns the ordered list of specs and derives
every previously-hardcoded table on the registry:

- ``AGNO_FUNCTION_TO_REGISTRY_NAME`` — the LLM's function-name to the
  registry name (e.g. ``read_file`` -> ``Read``). Derived by
  iterating each spec's ``agno_function_names``.
- ``VALID_EPHEMERAL_TOOL_NAMES`` — the set of registry names an
  ephemeral agent may request. Derived from each spec's ``name`` plus
  any aliases (e.g. ``BashOutput`` collapses to the ``Bash`` spec).

The catalog is the single source of truth. A new tool needs one
``ToolSpec`` subclass plus one entry in :meth:`ToolSpecCatalog.default`;
no separate table maintenance is required.

Result-shape ``NormalizeResult`` is used internally; the legacy
``raise-on-invalid`` shape is preserved by a thin shim on
:class:`ToolRegistry` for back-compat with existing ephemeral callers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar

from agno.tools import Toolkit
from agno.tools.file import FileTools
from agno.tools.python import PythonTools
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ember_code.core.tools.codeindex import CodeIndexTools
from ember_code.core.tools.edit import EmberEditTools, FileEditNotifier
from ember_code.core.tools.notebook import NotebookTools
from ember_code.core.tools.schedule import ScheduleTools
from ember_code.core.tools.search import GlobTools, GrepTools
from ember_code.core.tools.shell import EmberShellTools
from ember_code.core.tools.visualize import BroadcastFn, VisualizeTools
from ember_code.core.tools.web import WebTools

# Module-top optional import — replaces the inline import in the old
# ``_make_web_search``. The sentinel-``None`` pattern keeps import-time
# import-failure free when the extra isn't installed; ``WebSearchSpec``
# checks the sentinel on ``build`` and raises the same ``ImportError``.
try:
    from agno.tools.duckduckgo import DuckDuckGoTools
except ImportError:  # pragma: no cover — optional extra
    DuckDuckGoTools = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


class ToolBuildContext(BaseModel):
    """Frozen state passed to each spec's ``build`` call.

    Centralises the per-registry state a spec might need (base dir,
    session broadcast callable, cloud credentials) so specs get a
    single argument instead of a growing parameter list. New future
    state (e.g. cloud tokens for spec X) is added here without
    touching every spec's signature.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    base_dir: Path
    broadcast: BroadcastFn | None = None
    cloud_token: str | None = None
    cloud_server_url: str = "https://api.ignite-ember.sh"
    # Shared notifier for the file-edit push channel. ``None`` lets
    # ``EmberEditTools`` fall back to
    # :data:`ember_code.core.tools.edit.default_file_edit_notifier`,
    # the module-level default the backend also wires to by default.
    file_edit_notifier: FileEditNotifier | None = None


class NormalizeResult(BaseModel):
    """Result of :meth:`ToolSpecCatalog.normalize`.

    Result-shape (Pattern 3). Callers may still consume the legacy
    raise-shape via :meth:`ToolRegistry.normalize_agno_names`.
    """

    ok: bool
    names: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)

    def as_error_message(self) -> str:
        """Legacy error string for the raise-shape shim."""
        return (
            f"Unknown tool(s): {', '.join(self.invalid)}. "
            f"Available: {', '.join(sorted(self.available))}"
        )


class ToolResolutionRequest(BaseModel):
    """Normalised input for :meth:`ToolSpecCatalog.resolve` / equivalent
    registry resolve.

    Accepts either a comma-separated ``str`` or a ``list[str]`` and
    canonicalises to a list at the model boundary. Keeps the legacy
    string-splat path off the resolve implementation.
    """

    tool_names: list[str] = Field(default_factory=list)

    @field_validator("tool_names", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise TypeError(f"tool_names must be str or list[str], got {type(value).__name__}")


class ToolResolutionResult(BaseModel):
    """Verbose result of resolving a request through the catalog.

    ``tools`` are Agno ``Toolkit`` instances — not Pydantic-serialisable
    so we allow arbitrary types and skip serialisation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tools: list[Any] = Field(default_factory=list)
    skipped_denied: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unknown


class ToolSpec(BaseModel):
    """Declarative row describing one built-in tool.

    Subclasses override :meth:`build` for anything that isn't a plain
    ``toolkit_cls(**static_kwargs, base_dir=..., requires_confirmation_tools=...)``
    call — e.g. :class:`VisualizeSpec` needs the session broadcast,
    :class:`WebSearchSpec` guards on an optional import.

    Every attribute is a Pydantic field (not a class attribute), so
    ``.model_dump()`` on a catalog spec yields a real machine-readable
    description usable by tests / introspection.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    aliases: tuple[str, ...] = ()
    agno_function_names: tuple[str, ...] = ()
    confirm_function_names: tuple[str, ...] = ()
    toolkit_cls: type[Toolkit]
    static_kwargs: dict[str, Any] = Field(default_factory=dict)
    # ``"base_dir"``, ``"project_dir"``, or ``None`` for toolkits that
    # don't take a base directory (currently: web search, web fetch).
    base_dir_kwarg: str | None = "base_dir"
    # When ``True``, the base directory is passed as ``str(path)``;
    # when ``False``, the raw ``Path`` is passed (FileTools uses this
    # form because it does its own path resolution).
    base_dir_as_str: bool = True
    # When ``False``, this spec is available via the registry (e.g. main
    # agent gets it via ``resolve``) but NOT via ephemeral-agent
    # ``.md``-frontmatter ``tools:`` lists. Preserves the historical
    # ``VALID_EPHEMERAL_TOOL_NAMES`` set — ``CodeIndex`` and
    # ``Visualize`` are built-ins added by the main agent, not
    # requestable by user-authored ephemeral agents.
    ephemeral_visible: bool = True

    def build(self, context: ToolBuildContext, confirm: bool) -> Toolkit:
        """Default build: merge static kwargs, inject base dir, apply
        confirmation gating, instantiate.

        Subclasses override this when they need extra state (broadcast,
        optional-import guards, etc.).
        """
        kwargs = self._build_kwargs(context, confirm)
        return self.toolkit_cls(**kwargs)

    def _build_kwargs(self, context: ToolBuildContext, confirm: bool) -> dict[str, Any]:
        """Compose the ``__init__`` kwargs for ``toolkit_cls``.

        Extracted so subclasses can massage the dict (e.g. drop the
        confirm list for read-only tools) without re-implementing the
        base-dir plumbing.
        """
        kwargs: dict[str, Any] = dict(self.static_kwargs)
        if self.base_dir_kwarg:
            kwargs[self.base_dir_kwarg] = (
                str(context.base_dir) if self.base_dir_as_str else context.base_dir
            )
        if confirm and self.confirm_function_names:
            kwargs["requires_confirmation_tools"] = list(self.confirm_function_names)
        return kwargs


# ── Built-in spec subclasses ──────────────────────────────────────
# Each subclass captures a "kind of tool" — the shape of the kwargs
# recipe. Splitting into subclasses (rather than one giant table)
# addresses the audit's ``utility-module-of-related-helpers`` and
# ``dispatch-dict-of-free-functions`` findings: variation lives in
# subclass overrides, not in a flag-heavy build path.


class ReadFileSpec(ToolSpec):
    """Read-only ``FileTools`` — read/list ops, no writes.

    ``FileTools`` takes a raw ``Path`` for ``base_dir`` (not the
    stringified form the shell-family toolkits use).
    """

    name: str = "Read"
    agno_function_names: tuple[str, ...] = ("read_file", "read_file_chunk", "list_files")
    confirm_function_names: tuple[str, ...] = ("read_file", "list_files")
    toolkit_cls: type[Toolkit] = FileTools
    static_kwargs: dict[str, Any] = Field(
        default_factory=lambda: dict(
            enable_read_file=True,
            enable_save_file=False,
            enable_list_files=True,
            enable_search_files=False,
            enable_read_file_chunk=True,
            enable_replace_file_chunk=False,
            enable_search_content=False,
        )
    )
    base_dir_as_str: bool = False


class WriteFileSpec(ToolSpec):
    """Write-only ``FileTools`` — the ``save_file`` half of the read
    spec's toolkit."""

    name: str = "Write"
    agno_function_names: tuple[str, ...] = ("save_file",)
    confirm_function_names: tuple[str, ...] = ("save_file",)
    toolkit_cls: type[Toolkit] = FileTools
    static_kwargs: dict[str, Any] = Field(
        default_factory=lambda: dict(
            enable_read_file=False,
            enable_save_file=True,
            enable_list_files=False,
            enable_search_files=False,
            enable_read_file_chunk=False,
            enable_replace_file_chunk=False,
            enable_search_content=False,
        )
    )
    base_dir_as_str: bool = False


class EditSpec(ToolSpec):
    name: str = "Edit"
    agno_function_names: tuple[str, ...] = (
        "edit_file",
        "edit_file_replace_all",
        "create_file",
    )
    confirm_function_names: tuple[str, ...] = (
        "edit_file",
        "edit_file_replace_all",
        "create_file",
    )
    toolkit_cls: type[Toolkit] = EmberEditTools

    def build(self, context: ToolBuildContext, confirm: bool) -> Toolkit:
        # Inject the registry-owned notifier when the context carries
        # one. When ``None``, ``EmberEditTools`` falls back to the
        # module-level :data:`default_file_edit_notifier` — same
        # shared instance the push bridge binds to by default.
        # Mirrors ``VisualizeSpec.build`` which threads the broadcast
        # callable from the same context.
        kwargs = self._build_kwargs(context, confirm)
        if context.file_edit_notifier is not None:
            kwargs["notifier"] = context.file_edit_notifier
        return EmberEditTools(**kwargs)


class BashSpec(ToolSpec):
    """Shell tool. Aliased under ``BashOutput`` (the same toolkit
    provides both the run and the drain-buffer functions)."""

    name: str = "Bash"
    aliases: tuple[str, ...] = ("BashOutput",)
    agno_function_names: tuple[str, ...] = ("run_shell_command",)
    confirm_function_names: tuple[str, ...] = ("run_shell_command", "stop_process")
    toolkit_cls: type[Toolkit] = EmberShellTools


class LSSpec(ToolSpec):
    """``LS`` uses the same shell toolkit as :class:`BashSpec` but with
    no confirmation gating — listing is read-only. A dedicated subclass
    (rather than sharing BashSpec's confirm list) prevents the shell
    HITL from firing on directory listings.
    """

    name: str = "LS"
    # No ``agno_function_names`` — ``LS`` doesn't add new function
    # names to the LLM's function-to-registry mapping; the ephemeral
    # path recognises ``LS`` as a registry name directly.
    agno_function_names: tuple[str, ...] = ()
    confirm_function_names: tuple[str, ...] = ()
    toolkit_cls: type[Toolkit] = EmberShellTools

    def build(self, context: ToolBuildContext, confirm: bool) -> Toolkit:
        # Force ``confirm=False`` — LS must never gate. Explicit override
        # so a future bug (e.g. someone flipping the confirm default)
        # can't silently start prompting on LS calls.
        return super().build(context, confirm=False)


class GrepSpec(ToolSpec):
    name: str = "Grep"
    agno_function_names: tuple[str, ...] = ("grep", "grep_files", "grep_count")
    confirm_function_names: tuple[str, ...] = ("grep", "grep_files", "grep_count")
    toolkit_cls: type[Toolkit] = GrepTools


class GlobSpec(ToolSpec):
    name: str = "Glob"
    agno_function_names: tuple[str, ...] = ("glob_files",)
    confirm_function_names: tuple[str, ...] = ("glob_files",)
    toolkit_cls: type[Toolkit] = GlobTools


class WebSearchSpec(ToolSpec):
    """Web search via the optional ``duckduckgo-search`` extra.

    Overrides :meth:`build` to raise a helpful ``ImportError`` when the
    extra isn't installed — replaces the inline try/except import at
    the old ``_make_web_search`` call site (Rule 2 violation).
    """

    name: str = "WebSearch"
    agno_function_names: tuple[str, ...] = ("web_search", "search_news")
    confirm_function_names: tuple[str, ...] = ("duckduckgo_search", "duckduckgo_news")
    # ``DuckDuckGoTools`` doesn't accept a base_dir.
    base_dir_kwarg: str | None = None
    # ``toolkit_cls`` is populated at import time; ``None`` sentinel
    # when the optional extra isn't installed. We override ``build`` to
    # raise before the base implementation touches ``toolkit_cls``.
    toolkit_cls: type[Toolkit] = FileTools  # placeholder; overridden below

    def build(self, context: ToolBuildContext, confirm: bool) -> Toolkit:
        if DuckDuckGoTools is None:
            raise ImportError(
                "Web search requires duckduckgo-search. Install: pip install ember-code[web]"
            )
        kwargs: dict[str, Any] = {}
        if confirm:
            kwargs["requires_confirmation_tools"] = list(self.confirm_function_names)
        return DuckDuckGoTools(**kwargs)


class WebFetchSpec(ToolSpec):
    name: str = "WebFetch"
    agno_function_names: tuple[str, ...] = ("fetch_url", "fetch_json")
    confirm_function_names: tuple[str, ...] = ("fetch_url", "fetch_json")
    toolkit_cls: type[Toolkit] = WebTools
    base_dir_kwarg: str | None = None


class PythonSpec(ToolSpec):
    name: str = "Python"
    # No LLM-function aliases for Python — the ephemeral path takes the
    # registry name directly.
    agno_function_names: tuple[str, ...] = ()
    confirm_function_names: tuple[str, ...] = ("run_python_code",)
    toolkit_cls: type[Toolkit] = PythonTools


class ScheduleSpec(ToolSpec):
    """Schedule tool. Takes ``project_dir`` (not ``base_dir``) and
    never gates on confirm (schedule ops are read/write on the cron
    store, but the HITL prompt would fire on the LLM tool call rather
    than the underlying kernel action — deemed unnecessary today).
    """

    name: str = "Schedule"
    agno_function_names: tuple[str, ...] = (
        "schedule_task",
        "list_scheduled_tasks",
        "cancel_scheduled_task",
    )
    confirm_function_names: tuple[str, ...] = ()
    toolkit_cls: type[Toolkit] = ScheduleTools
    base_dir_kwarg: str | None = "project_dir"


class NotebookSpec(ToolSpec):
    name: str = "NotebookEdit"
    agno_function_names: tuple[str, ...] = (
        "notebook_read",
        "notebook_read_cell",
        "notebook_edit_cell",
        "notebook_add_cell",
        "notebook_remove_cell",
    )
    confirm_function_names: tuple[str, ...] = (
        "notebook_edit_cell",
        "notebook_add_cell",
        "notebook_remove_cell",
    )
    toolkit_cls: type[Toolkit] = NotebookTools


class CodeIndexSpec(ToolSpec):
    name: str = "CodeIndex"
    # CodeIndex is not exposed to ephemeral agents by function-name
    # aliasing today — it's a registry-level name only.
    agno_function_names: tuple[str, ...] = ()
    confirm_function_names: tuple[str, ...] = (
        "codeindex_search",
        "codeindex_item",
        "codeindex_references",
        "codeindex_commits",
    )
    toolkit_cls: type[Toolkit] = CodeIndexTools
    base_dir_kwarg: str | None = "project_dir"
    # Not reachable via ephemeral ``tools:`` frontmatter — matches the
    # historical ``VALID_EPHEMERAL_TOOL_NAMES`` which excluded it.
    ephemeral_visible: bool = False


class VisualizeSpec(ToolSpec):
    """Visualize tool. Overrides :meth:`build` to inject the
    registry-owned broadcast callable (proper subclass polymorphism,
    replacing the ``confirm``-ignored hack in the old
    ``_make_visualize``).
    """

    name: str = "Visualize"
    agno_function_names: tuple[str, ...] = ()
    confirm_function_names: tuple[str, ...] = ()
    toolkit_cls: type[Toolkit] = VisualizeTools
    base_dir_kwarg: str | None = None
    # Not reachable via ephemeral ``tools:`` frontmatter — matches the
    # historical ``VALID_EPHEMERAL_TOOL_NAMES`` which excluded it.
    ephemeral_visible: bool = False

    def build(self, context: ToolBuildContext, confirm: bool) -> Toolkit:
        # ``confirm`` unused — Visualize only sends a one-way UI payload
        # to the FE; nothing to gate. The broadcast is bound at registry
        # construction so headless / test contexts get a no-op tool
        # instead of an import-time failure.
        return VisualizeTools(broadcast=context.broadcast)


class ToolSpecCatalog:
    """Ordered catalog of :class:`ToolSpec` rows.

    Owns the derived tables that used to live as ``ClassVar`` on
    :class:`ToolRegistry`:

    - :attr:`by_name` — every registry name (spec name + aliases)
      -> the spec. ``catalog.by_name['BashOutput']`` returns the
      ``BashSpec`` instance, canonicalising the caller's alias.
    - :attr:`agno_to_registry` — LLM function name -> registry name.
    - :attr:`valid_ephemeral_names` — the frozen set of registry names
      an ephemeral agent may request.

    Instances are mutable via :meth:`register` (for custom user
    tools registered at runtime). The cached derived tables are
    invalidated on register.
    """

    # Registry names the resolver skips silently — they're pseudo-tools
    # handled elsewhere in the agent builder (Orchestrate ->
    # ``OrchestrateTools``, Knowledge -> ``KnowledgeTools``) rather than
    # by the built-in registry. Kept on the catalog so ``resolve``
    # doesn't need a magic-string check.
    RESERVED_NON_TOOL_NAMES: ClassVar[frozenset[str]] = frozenset({"Orchestrate", "Knowledge"})

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs: list[ToolSpec] = list(specs)
        self._validate_no_duplicate_functions()

    def _validate_no_duplicate_functions(self) -> None:
        """Guard: no two specs may claim the same Agno function name.

        ``AGNO_FUNCTION_TO_REGISTRY_NAME`` implicitly maps one function
        to one registry name; making that structural prevents a subtle
        override bug where two specs both claim ``read_file``.
        """
        seen: dict[str, str] = {}
        for spec in self._specs:
            for func in spec.agno_function_names:
                if func in seen:
                    raise ValueError(
                        f"Duplicate agno function name '{func}' claimed by "
                        f"specs '{seen[func]}' and '{spec.name}'"
                    )
                seen[func] = spec.name

    def _invalidate_caches(self) -> None:
        for attr in ("by_name", "agno_to_registry", "valid_ephemeral_names"):
            self.__dict__.pop(attr, None)

    @cached_property
    def by_name(self) -> Mapping[str, ToolSpec]:
        """Registry name -> spec, including aliases."""
        index: dict[str, ToolSpec] = {}
        for spec in self._specs:
            index[spec.name] = spec
            for alias in spec.aliases:
                index[alias] = spec
        return index

    @cached_property
    def agno_to_registry(self) -> Mapping[str, str]:
        """Agno LLM function name -> canonical registry name."""
        index: dict[str, str] = {}
        for spec in self._specs:
            for func in spec.agno_function_names:
                index[func] = spec.name
        return index

    @cached_property
    def valid_ephemeral_names(self) -> frozenset[str]:
        """Every ephemeral-visible registry name plus every alias — the
        set an ephemeral-agent ``tools:`` list may draw from.

        Specs with ``ephemeral_visible=False`` (currently ``CodeIndex``
        and ``Visualize``) are excluded — those are wired by the main
        agent path, not by user-authored ephemeral ``.md`` files.
        """
        names: set[str] = set()
        for spec in self._specs:
            if not spec.ephemeral_visible:
                continue
            names.add(spec.name)
            names.update(spec.aliases)
        return frozenset(names)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs)

    @property
    def registry_names(self) -> list[str]:
        """Sorted list of registry-visible names (aliases excluded).

        Matches the semantics of the old ``ToolRegistry.available_tools``
        which iterated ``_factories.keys()`` — aliases were included
        there (both ``Bash`` and ``BashOutput``). To preserve that
        surface exactly, callers wanting the alias-inclusive list
        should use :attr:`by_name` ``.keys()`` sorted.
        """
        return sorted(spec.name for spec in self._specs)

    @property
    def registry_names_with_aliases(self) -> list[str]:
        """Sorted registry names including every alias — matches the
        old ``sorted(_factories.keys())`` behaviour."""
        return sorted(self.by_name.keys())

    def register(self, spec: ToolSpec) -> None:
        """Add a spec at runtime. Invalidates derived caches."""
        self._specs.append(spec)
        self._invalidate_caches()
        self._validate_no_duplicate_functions()

    def normalize(self, names: list[str]) -> NormalizeResult:
        """Map Agno function names -> registry names, dedupe, validate.

        Returns a :class:`NormalizeResult`. ``MCP:*`` entries pass
        through unchanged (MCP servers connect after registry init).
        """
        mapped = [self.agno_to_registry.get(name, name) for name in names]
        deduped = list(dict.fromkeys(mapped))
        invalid = [
            name
            for name in deduped
            if name not in self.valid_ephemeral_names and not name.startswith("MCP:")
        ]
        return NormalizeResult(
            ok=not invalid,
            names=deduped,
            invalid=invalid,
            available=sorted(self.valid_ephemeral_names),
        )

    @classmethod
    def default(cls) -> ToolSpecCatalog:
        """Build the built-in catalog (order matches the old
        ``_factories`` dict insertion order for stability)."""
        return cls(
            [
                ReadFileSpec(),
                WriteFileSpec(),
                EditSpec(),
                BashSpec(),
                GrepSpec(),
                GlobSpec(),
                LSSpec(),
                WebSearchSpec(),
                WebFetchSpec(),
                PythonSpec(),
                ScheduleSpec(),
                NotebookSpec(),
                CodeIndexSpec(),
                VisualizeSpec(),
            ]
        )
