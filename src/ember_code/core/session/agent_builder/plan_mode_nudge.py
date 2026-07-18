"""Plan-mode nudge — loads the plan-mode prose from bundled prompts.

The plan-mode instructions used to be an 84-line embedded string
inside ``build_main_agent``. That coupled prose editing to Python
source edits and made the file a mixed-concern blob. The prose now
lives in ``core/prompts/plan_mode.base.md`` (+ the CodeIndex
variant extension), matching how ``main_agent.md`` is stored. The
:class:`PlanModeNudge` model captures the branching condition
(``codeindex_available``) as data and exposes ``.render()`` for the
instructions builder.
"""

from __future__ import annotations

from pydantic import BaseModel

from ember_code.core.prompts import load_prompt


class PlanModeNudge(BaseModel):
    """Render the plan-mode instructions block appended to the main
    agent's ``instructions`` list.

    The base prose is always included. When ``codeindex_available``
    is ``True``, the CodeIndex-specific extension is concatenated
    after — nudging the model to lean on ``codeindex_query`` /
    ``codeindex_tree`` as the primary research surface during plan
    mode. Loading both from co-located markdown files keeps the
    prose out of the Python source and lets prompt authors iterate
    without touching the builder.
    """

    codeindex_available: bool = False

    def render(self) -> str:
        """Return the full plan-mode block (base + optional
        CodeIndex extension) as a single string."""
        base = load_prompt("plan_mode.base")
        if not self.codeindex_available:
            return base
        extension = load_prompt("plan_mode.codeindex")
        return f"{base}\n\n{extension}"
