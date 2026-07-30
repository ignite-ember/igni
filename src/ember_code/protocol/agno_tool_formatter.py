"""Format Agno tool-call events into TUI-friendly strings.

Replaces the pre-refactor free helpers ``format_tool_args`` +
``extract_result`` (which took an event as their state-first arg
and reached into it repeatedly). All logic is now on classes with
explicit collaborators:

* :class:`ToolCallFormatter` (ABC) — how to format one tool's args.
* :class:`DefaultFormatter` — the generic ``key=value`` fallback.
* :class:`SpawnFormatter` — the ``spawn_agent`` / ``spawn_team``
  special case (agent name + first-line task snippet).
* :class:`ToolCallFormatterRegistry` — owns the friendly-name map
  and dispatches ``tool_name → formatter`` polymorphically.
* :class:`AgnoToolEventFormatter` — the coordinator caller-facing
  surface with :meth:`args_summary`, :meth:`friendly_name`, and
  :meth:`extract_result`. Composes a registry + optional
  :class:`EditDiffRenderer` for the ``edit_file`` diff branch.

Note: this module deliberately does NOT import Agno at module
scope — construction and every public method access only Agno
event objects duck-typed via ``getattr``. The permission
evaluator's lazy import path (``core/config/permission_eval.py``)
still needs the registry to be creatable without loading Agno.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ember_code.protocol.messages import ToolResultData

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ember_code.protocol.edit_diff_computer import EditDiffComputer


# ── Friendly-name catalog ─────────────────────────────────────────

# Snake_case Agno tool name → CC-style display name shown in the
# tool-call header. Two callers read this raw (permission_eval's
# lazy import, tests pinning the contract); everyone else routes
# through ``ToolCallFormatterRegistry.friendly_name``.
_DEFAULT_TOOL_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "read_file": "Read",
        "save_file": "Write",
        "edit_file": "Edit",
        "edit_file_replace_all": "Edit",
        "create_file": "Write",
        "run_shell_command": "Bash",
        "grep": "Grep",
        "grep_files": "Grep",
        "grep_count": "Grep",
        "glob_files": "Glob",
        "list_files": "LS",
        "duckduckgo_search": "WebSearch",
        "duckduckgo_news": "WebSearch",
        "fetch_url": "WebFetch",
        "fetch_json": "WebFetch",
        "run_python_code": "Python",
        "spawn_agent": "Agent",
        "spawn_team": "Team",
        "delegate_task_to_member": "Delegate",
        "delegate_task_to_members": "Delegate",
        "search_knowledge_base": "Knowledge",
        "update_user_memory": "Memory",
        "schedule_task": "Schedule",
        "list_scheduled_tasks": "Schedule",
        "cancel_scheduled_task": "Schedule",
    }
)


# ── ToolCallFormatter hierarchy ───────────────────────────────────


class ToolCallFormatter:
    """ABC for per-tool arg-summary formatters.

    Concrete subclasses override :meth:`format_args`. The registry
    routes each tool name to one instance (or falls back to the
    default). The pre-refactor if/elif on ``tool_name`` collapses
    to a dict lookup + polymorphic dispatch.
    """

    def format_args(self, tool_args: dict[str, Any]) -> str:  # pragma: no cover - ABC
        raise NotImplementedError


class DefaultFormatter(ToolCallFormatter):
    """Generic ``key=value, ...`` summary, capped at 3 entries.

    Values over 30 chars are truncated with ``...``. Matches the
    pre-refactor free-function fallback path exactly (tested in
    ``test_agno_events.py::TestFormatToolArgsGeneric``).
    """

    _MAX_ENTRIES = 3
    _MAX_VALUE_LEN = 30
    _TRUNCATE_AT = 27  # 27 + len("...") == 30

    def format_args(self, tool_args: dict[str, Any]) -> str:
        parts: list[str] = []
        for k, v in list(tool_args.items())[: self._MAX_ENTRIES]:
            val = str(v)
            if len(val) > self._MAX_VALUE_LEN:
                val = val[: self._TRUNCATE_AT] + "..."
            parts.append(f"{k}={val}")
        return ", ".join(parts)


class SpawnFormatter(ToolCallFormatter):
    """Special-case ``spawn_agent`` / ``spawn_team``.

    Their ``task`` arg is often a multi-paragraph markdown brief;
    showing it verbatim drowns the tool-call header and leaks raw
    markdown into the terminal. This formatter surfaces the agent
    name + optional mode + first non-empty line of the task
    (capped at 80 chars).
    """

    _TASK_MAX_LEN = 80
    _TASK_TRUNCATE_AT = 77

    def format_args(self, tool_args: dict[str, Any]) -> str:
        agent = self._agent_display(tool_args)
        task_line = self._first_task_line(tool_args)
        mode = tool_args.get("mode", "")

        parts: list[str] = []
        if agent:
            parts.append(agent)
        if mode:
            parts.append(f"mode={mode}")
        if task_line:
            parts.append(f'"{task_line}"')
        return ", ".join(parts)

    @staticmethod
    def _agent_display(tool_args: dict[str, Any]) -> str:
        """Coerce ``agent_name`` (str) or ``agent_names`` (list) to
        a single comma-joined display string.

        ``spawn_team`` passes a list; ``spawn_agent`` passes a
        scalar. Without this coercion the pre-refactor code hit a
        TypeError on the list path — pinned by
        ``test_spawn_team_joins_agent_names_list``.
        """
        raw = tool_args.get("agent_name") or tool_args.get("agent_names") or ""
        if isinstance(raw, (list, tuple)):
            return ", ".join(str(n) for n in raw)
        return str(raw)

    def _first_task_line(self, tool_args: dict[str, Any]) -> str:
        """First non-empty line of the task arg, truncated to 80."""
        task = str(tool_args.get("task", ""))
        first_line = next((ln.strip() for ln in task.splitlines() if ln.strip()), "")
        if len(first_line) > self._TASK_MAX_LEN:
            first_line = first_line[: self._TASK_TRUNCATE_AT] + "..."
        return first_line


# ── Registry ─────────────────────────────────────────────────────


class ToolCallFormatterRegistry:
    """Owns the friendly-name catalog and the tool_name → formatter
    routing table.

    Mutable via :meth:`register`, but the default construction is
    already the production shape — external plugins that want to
    add a special-case formatter for their own tool call
    :meth:`register` explicitly (or subclass this registry).

    The friendly-name map is exposed via :attr:`friendly_names`
    as a read-only :class:`MappingProxyType` so ``permission_eval``
    can iterate it without importing Agno and without risking
    mutation.
    """

    def __init__(self) -> None:
        self._default_formatter: ToolCallFormatter = DefaultFormatter()
        self._formatters: dict[str, ToolCallFormatter] = {}
        self._names: dict[str, str] = dict(_DEFAULT_TOOL_NAMES)

        # Register the two orchestration tools that get the spawn
        # special-case. Any new tool with the same shape (task +
        # agent_name) can be routed to the same instance.
        spawn = SpawnFormatter()
        self.register("spawn_agent", spawn)
        self.register("spawn_team", spawn)

    def register(self, tool_name: str, formatter: ToolCallFormatter) -> None:
        """Route ``tool_name`` to ``formatter`` for arg formatting.

        Idempotent — a second registration overwrites (last write
        wins). Callers wire this at boot; concurrent registration
        during a run is not supported.
        """
        self._formatters[tool_name] = formatter

    def formatter_for(self, tool_name: str) -> ToolCallFormatter:
        """Return the formatter that handles ``tool_name``.

        Falls back to :class:`DefaultFormatter` when no specific
        registration matches — matches the pre-refactor generic
        path.
        """
        return self._formatters.get(tool_name, self._default_formatter)

    def format_args(self, tool_name: str, tool_args: dict | None) -> str:
        """Format ``tool_args`` for ``tool_name`` via the routed
        formatter. Empty / non-dict args short-circuit to ``""``
        (defensive — the header is on the hot path and a TypeError
        here would kill the streaming display)."""
        if not tool_args or not isinstance(tool_args, dict):
            return ""
        return self.formatter_for(tool_name).format_args(tool_args)

    def friendly_name(self, tool_name: str, default: str | None = None) -> str:
        """Look up the display name for a raw Agno tool name.

        Unknown tools fall back to ``default`` (or the raw name
        when default is None) so the TUI never shows an empty
        pill for a custom tool.
        """
        if default is None:
            default = tool_name
        return self._names.get(tool_name, default)

    @property
    def friendly_names(self) -> Mapping[str, str]:
        """Read-only view of the raw ``name → friendly`` map.

        Exposed for ``permission_eval`` which builds a reverse
        index (``Bash → {run_shell_command, ...}``) and for tests
        pinning the catalog contract. The proxy prevents accidental
        mutation of the shared registry state.
        """
        return MappingProxyType(self._names)


# Module-level default registry — the coordinator constructs this
# lazily so callers that only need friendly names (permission_eval)
# don't pay to boot the spawn special-case. Instances are cheap
# (a dict + two formatters) so this is more about explicit ownership
# than performance.
_default_registry: ToolCallFormatterRegistry | None = None


def default_registry() -> ToolCallFormatterRegistry:
    """Return the process-wide default registry, constructing on
    first access.

    Every call site that does not want to inject its own registry
    (server_history_walker, hitl_stream_mux, pause_handler,
    permission_eval) reaches through this getter — a single seam
    where a test can override with a bespoke registry via monkeypatch
    if that becomes useful.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolCallFormatterRegistry()
    return _default_registry


# ── Coordinator ──────────────────────────────────────────────────


class AgnoToolEventFormatter:
    """Coordinator: turn an Agno tool event into TUI-friendly parts.

    Composes a :class:`ToolCallFormatterRegistry` plus an optional
    :class:`EditDiffComputer` (only needed for the ``edit_file``
    branch of :meth:`extract_result`). Callers can either construct
    once and reuse (the serializer does this) or use the module-
    level :func:`default_formatter` singleton.
    """

    def __init__(
        self,
        registry: ToolCallFormatterRegistry | None = None,
        diff_computer: EditDiffComputer | None = None,
    ) -> None:
        self._registry = registry or default_registry()
        self._diff_computer = diff_computer

    # ── Public API ───────────────────────────────────────────

    def args_summary(self, tool_name: str, tool_args: dict | None) -> str:
        """Format ``tool_args`` for the tool-call header."""
        return self._registry.format_args(tool_name, tool_args)

    def friendly_name(self, tool_name: str, default: str | None = None) -> str:
        """Return the display name for an Agno tool."""
        return self._registry.friendly_name(tool_name, default=default)

    @property
    def registry(self) -> ToolCallFormatterRegistry:
        """Expose the underlying registry for callers that need
        the friendly-names view (permission_eval)."""
        return self._registry

    def extract_result(self, event: Any) -> ToolResultData:
        """Extract a :class:`ToolResultData` from a completed tool
        event.

        Handles the ``edit_file`` diff branch (composing with
        :class:`EditDiffRenderer` when injected — otherwise treats
        the tool as unknown and falls through to the summary
        path). Preserves every pinned behavior from the pre-
        refactor free function:

        * ``"None"`` / ``"null"`` / ``"undefined"`` → empty result.
        * Single-line results truncated at 80 chars.
        * Multi-line results collapsed to ``"N lines of output"``.
        * ``"completed in <timing>"`` fallback when no summary text.
        * Failed edits (``Error:`` prefix) skip the diff branch so
          the error prefix survives
          :class:`~ember_code.protocol.tool_error_conventions.ToolResultErrorDetector`
          detection.
        """
        tool = getattr(event, "tool", None)
        timing = self._extract_timing(tool)
        result = getattr(tool, "result", None) if tool else None
        tool_name = getattr(tool, "tool_name", "?") if tool else "?"

        logger.debug(
            "extract_result [%s]: result type=%s, is_none=%s, len=%d",
            tool_name,
            type(result).__name__,
            result is None,
            len(str(result)) if result is not None else 0,
        )

        result_str = str(result).strip() if result else ""

        # Diff branch: only when the diff computer is available AND
        # this is an edit_file success (see docstring for the
        # ``Error:`` skip rationale).
        if (
            self._diff_computer is not None
            and tool_name == "edit_file"
            and tool is not None
            and not result_str.startswith("Error:")
        ):
            rows = self._diff_computer.compute(getattr(tool, "tool_args", None))
            if rows:
                summary_msg = result_str or "Edited"
                if timing:
                    summary_msg = f"{summary_msg}, {timing}"
                return ToolResultData.from_edit_diff(
                    summary=summary_msg,
                    diff_rows=rows,
                )

        # Non-diff / non-edit path.
        full_text = self._normalize_full_text(str(result).strip() if result else "")
        summary = self._compose_summary(full_text, timing)
        return ToolResultData(summary=summary, full_result=full_text)

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_timing(tool: Any) -> str:
        """Read the tool's duration metric and format it as ``X.XXs``.

        Returns ``""`` when no metrics are attached. Kept as a static
        helper so ``extract_result`` reads top-to-bottom without
        an inline metrics-walk block.
        """
        if not tool:
            return ""
        metrics = getattr(tool, "metrics", None)
        if not metrics:
            return ""
        duration = getattr(metrics, "duration", None)
        if duration is None:
            return ""
        return f"{duration:.2f}s"

    @staticmethod
    def _normalize_full_text(text: str) -> str:
        """MCP tools return literal ``"None"`` / ``"null"`` /
        ``"undefined"`` for empty responses. Treat all three as
        empty so the tool card doesn't show a misleading pill.
        """
        if text in ("None", "null", "undefined"):
            return ""
        return text

    @staticmethod
    def _compose_summary(full_text: str, timing: str) -> str:
        """Compose the collapsed-card summary line.

        * Single-line body → truncated at 80 chars.
        * Multi-line body → "N lines of output".
        * No body but timing → "completed in <timing>".
        * Body + timing → "<body>, <timing>".
        """
        summary = ""
        if full_text:
            lines = full_text.splitlines()
            if len(lines) <= 1:
                short = full_text[:80]
                summary = short + ("..." if len(full_text) > 80 else "")
            else:
                summary = f"{len(lines)} lines of output"

        if summary and timing:
            return f"{summary}, {timing}"
        if not summary and timing:
            return f"completed in {timing}"
        return summary


__all__ = [
    "AgnoToolEventFormatter",
    "DefaultFormatter",
    "SpawnFormatter",
    "ToolCallFormatter",
    "ToolCallFormatterRegistry",
    "default_registry",
]
