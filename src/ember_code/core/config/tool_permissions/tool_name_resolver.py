"""Agno function-name → catalog tool-name resolver.

Owns the ``FUNC_TO_TOOL`` mapping that used to live at module top
level in ``tool_permissions.py``. Wrapping the table in a class
means:

* The dict is a :class:`typing.ClassVar` — nobody outside the
  resolver can mutate it. The back-compat shim in
  ``tool_permissions/__init__.py`` exposes a :class:`MappingProxyType`
  view so imports of ``FUNC_TO_TOOL`` see the live table without
  being able to write to it.
* Callers that used to do ``FUNC_TO_TOOL.get(func_name, tool_name)``
  now call :meth:`ToolNameResolver.resolve` — a single method that
  captures the "prefer tool_name, fall back to func_name mapping"
  logic in one place. Previously that same pattern was open-coded
  in :meth:`ToolPermissions.check` AND twice inside
  ``backend/hitl_controller.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar


class ToolNameResolver:
    """Resolves catalog tool names from Agno function names.

    Stateless — the mapping is a class-level constant. Instances are
    cheap to create so callers can wire one into their orchestrator
    without worrying about lifecycle.
    """

    _MAP: ClassVar[dict[str, str]] = {
        "run_shell_command": "Bash",
        "read_file": "Read",
        "save_file": "Write",
        "list_files": "LS",
        "edit_file": "Edit",
        "replace_in_file": "Edit",
        "grep_search": "Grep",
        "glob_files": "Glob",
        "web_fetch": "WebFetch",
        "duckduckgo_search": "WebSearch",
        "duckduckgo_news": "WebSearch",
        "run_python_code": "Python",
        "notebook_read": "NotebookEdit",
        "notebook_read_cell": "NotebookEdit",
        "notebook_edit_cell": "NotebookEdit",
        "notebook_add_cell": "NotebookEdit",
        "notebook_remove_cell": "NotebookEdit",
    }

    def resolve(self, tool_name: str | None = None, func_name: str | None = None) -> str:
        """Return the catalog tool name for a call.

        Prefers ``tool_name`` when non-empty (the tool already knows
        who it is). Falls back to mapping ``func_name`` through the
        table. Falls back to ``func_name`` itself if the table has
        no entry — an unknown function is safer to name after itself
        than to silently pretend it's a well-known tool.
        """
        if tool_name:
            return tool_name
        if not func_name:
            return ""
        return self._MAP.get(func_name, func_name)

    def catalog_for(self, func_name: str) -> str | None:
        """Direct table lookup without the ``tool_name`` fallback —
        used by the back-compat ``FUNC_TO_TOOL`` shim so it behaves
        exactly like the dict it replaced."""
        return self._MAP.get(func_name)

    @classmethod
    def all_mappings(cls) -> Mapping[str, str]:
        """Read-only view of the underlying table.

        Returned as a :class:`MappingProxyType` so downstream code
        can't accidentally mutate the resolver's state. Used by the
        ``FUNC_TO_TOOL`` module-level shim in
        ``tool_permissions/__init__.py``.
        """
        return MappingProxyType(cls._MAP)
