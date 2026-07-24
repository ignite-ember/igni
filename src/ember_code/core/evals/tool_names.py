"""Tool-name expansion catalog for eval suites.

The registry uses display names like ``Read`` / ``Write`` / ``Bash``
in YAML for readability, but Agno's tool_calls contain the underlying
function names (``read_file``, ``save_file`` …). This module owns the
one-to-many table plus the "system tools Agno may auto-invoke that we
should never fail on" list.

Previously two module-level tables (``_TOOL_NAME_EXPANSIONS`` and
``_ALWAYS_ALLOWED_SYSTEM_TOOLS``) sat in ``runner.py`` alongside two
free functions that read them. That coupling is now a single class
whose defaults reproduce the original tables and can be overridden
per-suite or in tests.
"""

from __future__ import annotations

from typing import ClassVar


class ToolNameCatalog:
    """Owns the display-name to Agno-function-name expansion table.

    Maps registry display names (used in YAML for readability) to the
    real Agno function names that appear in tool_calls. Each display
    name expands to ALL of its toolkit's functions — listing ``Read``
    in ``expected_tool_calls`` means "read_file OR read_file_chunk OR
    list_files are all permitted." Unknown names (e.g. spawn_agent /
    spawn_team) pass through unchanged so orchestration tool names
    work without translation.
    """

    #: Class-level defaults; instances may override per-suite or in tests.
    DEFAULT_EXPANSIONS: ClassVar[dict[str, list[str]]] = {
        "Read": ["read_file", "read_file_chunk", "list_files"],
        "Write": ["save_file", "create_file"],
        "Edit": ["edit_file", "edit_file_replace_all"],
        "Bash": [
            "run_shell_command",
            "read_process_output",
            "watch_process",
            "stop_process",
            "list_processes",
        ],
        "Grep": ["grep", "grep_files", "grep_count"],
        "Glob": ["glob_files"],
        "LS": ["list_files"],
        "WebSearch": ["web_search", "search_news"],
        "WebFetch": ["ember_web", "fetch_url"],
        # All ScheduleTools functions — without this, ``Schedule`` falls
        # through unchanged and the matcher looks for a literal function
        # name "Schedule" that never exists, so positive cases that
        # *correctly* call ``schedule_task`` fail with "missing tool
        # calls: schedule_task" and anti-cases pass for the wrong reason
        # (no function literally named "Schedule" was ever going to
        # match, so the "unexpected" check was always trivially clean).
        "Schedule": [
            "schedule_task",
            "list_scheduled_tasks",
            "cancel_scheduled_task",
        ],
        # KnowledgeTools — pass through individual function names. Listed
        # here so eval YAMLs can also use the umbrella name "Knowledge"
        # if a case wants to allow any of the three operations.
        "Knowledge": [
            "knowledge_search",
            "knowledge_add",
            "knowledge_delete",
            "knowledge_status",
        ],
    }

    #: Tools that Agno may auto-invoke regardless of what the case asked
    #: for — they belong to the learning / memory subsystem and aren't
    #: part of the agent's deliberate plan. Including them in every
    #: expanded allowlist keeps the reliability check from false-failing
    #: when, e.g., ``update_user_memory`` fires during a tasks-mode run.
    #: Don't add tools here that the case is meant to *test* (file ops,
    #: spawn_*, etc.) — those should be explicit in the case YAML.
    DEFAULT_ALWAYS_ALLOWED_SYSTEM_TOOLS: ClassVar[tuple[str, ...]] = (
        "update_user_memory",
        "knowledge_add",
        "knowledge_search",
        "knowledge_status",
    )

    def __init__(
        self,
        expansions: dict[str, list[str]] | None = None,
        always_allowed_system_tools: tuple[str, ...] | None = None,
    ) -> None:
        self.expansions: dict[str, list[str]] = (
            dict(expansions) if expansions is not None else dict(self.DEFAULT_EXPANSIONS)
        )
        self.always_allowed_system_tools: tuple[str, ...] = (
            always_allowed_system_tools
            if always_allowed_system_tools is not None
            else self.DEFAULT_ALWAYS_ALLOWED_SYSTEM_TOOLS
        )

    def expand(self, names: list[str]) -> list[str]:
        """Expand display names to function names; pass unknown names through."""
        out: list[str] = []
        for n in names:
            for fn in self.expansions.get(n, [n]):
                if fn not in out:
                    out.append(fn)
        return out

    def expand_expected(self, names: list[str]) -> list[str]:
        """Like :meth:`expand` but extends with always-allowed system tools.

        Use this for the allowlist (``expected_tool_calls``), NOT for the
        blocklist (``unexpected_tool_calls``). Adding system tools to a
        blocklist would unintentionally forbid them everywhere.
        """
        out = self.expand(names)
        for fn in self.always_allowed_system_tools:
            if fn not in out:
                out.append(fn)
        return out


#: Module-level default catalog. Runners / drivers accept a catalog
#: constructor arg (defaulting to this) so tests can swap in a stubbed
#: version.
DEFAULT_CATALOG = ToolNameCatalog()
