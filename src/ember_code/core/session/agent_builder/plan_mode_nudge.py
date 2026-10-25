"""Plan-mode nudge — loads the plan-mode prose from bundled prompts.

The plan-mode instructions used to be an 84-line embedded string
inside ``build_main_agent``. That coupled prose editing to Python
source edits and made the file a mixed-concern blob. The prose now
lives in ``core/prompts/plan_mode.base.md`` (+ the CodeIndex
variant extension), matching how ``main_agent.md`` is stored. The
:class:`PlanModeNudge` model captures the branching condition
(``codeindex_available``) as data and exposes ``.render()`` for the
instructions builder.

It is the ``plan_mode`` prompt document: an extension may replace it,
replace a section of it, or add to its ``code_intelligence`` slot, as
it may the main prompt (:mod:`ember_code.core.prompts.document`).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.prompts import load_prompt
from ember_code.core.prompts.contributions import PromptContributions, prompt_contributions
from ember_code.core.prompts.document import PromptDocument, PromptDocumentName
from ember_code.core.prompts.fragments import fragments


class PlanModeNudge(BaseModel):
    """Render the plan-mode instructions block appended to the main
    agent's ``instructions`` list.

    The base prose is always included, as extensions have shaped it.
    When ``codeindex_available`` is ``True``, the CodeIndex-specific
    extension is concatenated after — nudging the model to lean on
    CodeIndex as the primary research surface during plan mode, via
    the ``data-architect`` specialist that holds ``codeindex_cypher``
    (the only agent-facing CodeIndex tool). Loading both from
    co-located markdown files keeps the prose out of the Python
    source and lets prompt authors iterate without touching the
    builder.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    codeindex_available: bool = False
    contributions: PromptContributions = Field(default_factory=lambda: prompt_contributions)

    def render(self) -> str:
        """Return the full plan-mode block (base + optional
        CodeIndex extension) as a single string."""
        document = PromptDocumentName.PLAN_MODE
        replaced = self.contributions.replacement(document)
        text = (
            fragments.expand(replaced) if replaced is not None else load_prompt(document.template)
        )
        sections = {
            name: fragments.expand(body)
            for name, body in self.contributions.section_replacements(document).items()
        }
        fills = {
            name: fragments.expand(body)
            for name, body in self.contributions.slot_fills(document).items()
        }
        base = PromptDocument(text).replace_sections(sections).fill_slots(fills).strip()
        if not self.codeindex_available:
            return base
        extension = load_prompt("plan_mode.codeindex")
        return f"{base}\n\n{extension}"
