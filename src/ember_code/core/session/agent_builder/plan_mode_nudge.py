"""Plan-mode nudge — loads the plan-mode prose from bundled prompts.

The plan-mode instructions used to be an 84-line embedded string
inside ``build_main_agent``. That coupled prose editing to Python
source edits and made the file a mixed-concern blob. The prose now
lives in ``core/prompts/plan_mode.base.md``, matching how
``main_agent.md`` is stored, and :class:`PlanModeNudge` exposes
``.render()`` for the instructions builder.

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

    The prose as extensions have shaped it: what CodeIndex says about
    researching through its index in plan mode arrives in the
    ``code_intelligence`` slot, while its index covers HEAD.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    contributions: PromptContributions = Field(default_factory=lambda: prompt_contributions)

    def render(self) -> str:
        """Return the full plan-mode block as a single string."""
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
        return PromptDocument(text).replace_sections(sections).fill_slots(fills).strip()
