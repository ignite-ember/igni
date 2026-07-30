"""Instructions-list assembly stage.

Owns everything that lands in the ``instructions=[...]`` kwarg
passed to ``Agent(...)`` *after* the base system prompt: project
instructions, active TODO, multi-workspace context, plan-mode
nudge, output style block, and the memory writeback block.

Each ``append_*`` method appends 0-1 entries; :meth:`assemble`
runs them in order. Keeping them as small methods on a single
class beats the 145-line inline procedural chain that used to
live in ``build_main_agent`` — each entry is now testable
individually and the append order is a single scanner-friendly
method body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ember_code.core.utils.context import (
    ProjectMemoryBank,
    load_project_context,
)

from .plan_mode_nudge import PlanModeNudge


class InstructionsBuilder:
    """Assemble the main agent's ``instructions`` list.

    Constructor takes named collaborators harvested from the
    session so the class is decoupled from :class:`Session`
    internals — the coordinator gathers them via the session's
    public accessors before instantiating.
    """

    def __init__(
        self,
        *,
        project_dir: Path,
        project_instructions: str,
        workspace: Any,
        settings: Any,
        output_styles: dict,
        active_output_style_name: str,
        codeindex_available: bool,
    ) -> None:
        self._project_dir = project_dir
        self._project_instructions = project_instructions
        self._workspace = workspace
        self._settings = settings
        self._output_styles = output_styles
        self._active_output_style_name = active_output_style_name
        self._codeindex_available = codeindex_available

    def append_project_instructions(self, out: list[str]) -> None:
        """Append the ember.md / CLAUDE.md project instructions."""
        if self._project_instructions:
            out.append(f"Project instructions:\n{self._project_instructions}")

    def append_todo(self, out: list[str]) -> None:
        """Append ``.ember/TODO.md`` if present (root only)."""
        todo_path = self._project_dir / ".ember" / "TODO.md"
        if not todo_path.is_file():
            return
        content = todo_path.read_text().strip()
        if content:
            out.append(f"Active TODO (.ember/TODO.md):\n{content}")

    def append_workspace_context(self, out: list[str]) -> None:
        """Append the multi-workspace context blocks.

        Primary workspace's context comes from the workspace
        manager's own ``get_context_instructions``; additional
        workspace dirs each get their own project-context load
        so the agent sees "Additional workspace (name): rules…".
        """
        workspace_ctx = self._workspace.get_context_instructions()
        if not workspace_ctx:
            return
        out.append(workspace_ctx)
        for extra_dir in self._workspace.additional_dirs:
            extra_rules = load_project_context(
                extra_dir,
                self._settings.context.project_file,
                read_claude_md=self._settings.rules.cross_tool_support,
            )
            if extra_rules:
                out.append(f"Additional workspace ({extra_dir.name}):\n{extra_rules}")

    def append_plan_mode_nudge(self, out: list[str]) -> None:
        """Append the plan-mode instructions block.

        Base prose is always included; the CodeIndex-variant
        extension is appended when the index is populated for the
        current HEAD (avoids telling the agent to use a tool
        whose results will be empty).
        """
        nudge = PlanModeNudge(codeindex_available=self._codeindex_available)
        out.append(nudge.render())

    def append_output_style(self, out: list[str]) -> None:
        """Append the active output-style body (row 52).

        Empty when no style is active or the style has no body —
        the agent falls back to bare model behaviour.
        """
        style = self._output_styles.get(self._active_output_style_name)
        if style and style.body:
            out.append(f"# Output style: {style.name}\n\n{style.body}")

    def append_memory_writeback(self, out: list[str]) -> None:
        """Append the memory writeback instructions (row 61).

        Teaches the agent WHEN and HOW to persist memories
        during this conversation. The READ side (loading
        existing MEMORY.md into the system prompt) landed with
        row 18; this is the WRITE side.
        """
        out.append(ProjectMemoryBank(self._project_dir).writeback_instructions())

    def assemble(self, base_prompt: str) -> list[str]:
        """Compose the full instructions list starting from
        ``base_prompt`` as the first entry."""
        out: list[str] = [base_prompt]
        self.append_project_instructions(out)
        self.append_todo(out)
        self.append_workspace_context(out)
        self.append_plan_mode_nudge(out)
        self.append_output_style(out)
        self.append_memory_writeback(out)
        return out
