"""System-prompt assembly stage.

Owns the base system prompt loaded from ``core/prompts/``: takes an
extension's replacement of the whole prompt when there is one, applies the sections
extensions replace, substitutes the ``{{AGENT_CATALOG}}`` placeholder
from the agent pool's catalog, fills the slots extensions add to,
resolves the capability-conditional spans in the markdown, and
tacks on the skill-descriptions block when skills are configured to
auto-trigger. What an extension may do to it is
:mod:`ember_code.core.prompts.document`.

The output is a single string handed to
:class:`InstructionsBuilder` as the first entry of the
``instructions`` list.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from ember_code.core.prompts.contributions import PromptContributions, prompt_contributions
from ember_code.core.prompts.document import NAME, PromptDocument, PromptDocumentName
from ember_code.core.prompts.fragments import fragments

#: ``<!-- slot.<id>:only -->``: guidance that depends on whether a slot
#: is filled and does not sit inside it.
_SLOT_CONDITIONAL = re.compile(rf"<!-- /?slot\.({NAME}):(?:only|absent) -->")

logger = logging.getLogger(__name__)


class PromptBuilder:
    """Render the main-agent system prompt.

    :meth:`render` is the only public entry point — the smaller
    helpers exist so each substitution step is testable in
    isolation and the render sequence reads as a short list of
    named phases.
    """

    #: Capability-conditional spans in the prompt markdown. ``only``
    #: survives when the capability is present, ``absent`` when it is
    #: not; the markers themselves never reach the model. Add a name
    #: here and a matching entry in :meth:`_capabilities` to make a new
    #: capability switchable. What extensions provide is not in this
    #: list: it arrives in slots, and ``slot.<id>`` is the conditional
    #: for whether one is filled.
    _CONDITIONALS: ClassVar[tuple[str, ...]] = (
        "user-memory",
        "can-persist",
    )

    def __init__(
        self,
        *,
        project_dir: Path,
        settings: Any,
        agent_catalog: str,
        skill_descriptions: str,
        prompt_loader: Callable[[str], str],
        contributions: PromptContributions | None = None,
        user_memory_available: bool = True,
    ) -> None:
        self._project_dir = project_dir
        self._settings = settings
        self._contributions = contributions if contributions is not None else prompt_contributions
        self._fills = {
            slot: fragments.expand(text)
            for slot, text in self._contributions.slot_fills(PromptDocumentName.MAIN).items()
        }
        self._user_memory_available = user_memory_available
        self._agent_catalog = agent_catalog
        self._skill_descriptions = skill_descriptions
        self._prompt_loader = prompt_loader

    def load_base(self) -> str:
        """Pick + load the base template.

        An extension that replaces the whole prompt wins: an enterprise
        that wants the agent to work its way gets exactly that, and the
        sections and slots below still apply to whatever it wrote.
        """
        replaced = self._contributions.replacement(PromptDocumentName.MAIN)
        if replaced is not None:
            return fragments.expand(replaced.strip())
        return self._prompt_loader("main_agent")

    def replace_sections(self, prompt: str) -> str:
        """The sections extensions replace, replaced."""
        sections = {
            name: fragments.expand(text)
            for name, text in self._contributions.section_replacements(
                PromptDocumentName.MAIN
            ).items()
        }
        return PromptDocument(prompt).replace_sections(sections).text

    def fill_slots(self, prompt: str) -> str:
        """Every slot: what extensions put there, or its default."""
        return PromptDocument(prompt).fill_slots(self._fills)

    def substitute_placeholders(self, prompt: str) -> str:
        """Substitute ``{{AGENT_CATALOG}}`` and ``{{PERSISTING}}``."""
        catalog = self._agent_catalog or "(no agents loaded)"
        prompt = prompt.replace("{{AGENT_CATALOG}}", catalog)
        prompt = prompt.replace("{{PERSISTING}}", self._load_section("persisting"))
        return prompt

    def _load_section(self, stem: str) -> str:
        """A prose section kept beside the prompt rather than inside it.

        Used where a block is long enough that inlining it buries the
        document's structure. The spans inside it are resolved by
        :meth:`resolve_conditionals` along with everything else, so a
        section file is a formatting choice and never a second
        mechanism.
        """
        try:
            return self._prompt_loader(stem).strip()
        except (FileNotFoundError, OSError):
            logger.warning("prompt section %r missing; omitting", stem)
            return ""

    def resolve_conditionals(self, prompt: str) -> str:
        """Keep the spans that match this session and drop the rest.

        A capability that can be absent needs both branches written and
        exactly one of them shown. ``<!-- project-documents:only -->``
        survives when something keeps the project's documents,
        ``<!-- project-documents:absent -->`` when nothing does, and the
        markers themselves never reach the model.

        Spans live in the markdown rather than in separate per-state
        files because the guidance is interleaved with guidance that
        stays either way. Splitting by state duplicates everything
        shared: the two halves of this section were briefly separate
        files and measured 49% identical, with the common paragraphs
        already drifting apart in wording after a single edit.

        The failure being prevented is specific and has shipped twice.
        ``knowledge_add`` / ``knowledge_search`` were taught across ~30
        unconditional lines while the toolkit was attached only when an
        index existed: 7,459 system prompts taught them, 0 manifests
        carried them, 0 calls were ever made. When the versioned store
        replaced the knowledge base the section was rewritten to
        ``artifact_add`` — the same mistake under the name the new
        subsystem introduced.

        A missing capability is not only a deletion. The ``absent``
        branch says what to do with a project convention when there is
        nowhere to file one, which is what keeps the prompt coherent
        rather than merely shorter.

        Unbalanced markers raise. A prompt that silently loses a
        section is the failure this method exists to prevent.
        """
        available = self._capabilities()
        for slot in sorted(set(_SLOT_CONDITIONAL.findall(prompt))):
            available[f"slot.{slot}"] = slot in self._fills
        for name in available:
            keep, drop = ("only", "absent") if available[name] else ("absent", "only")
            for branch in (keep, drop):
                open_tag = f"<!-- {name}:{branch} -->"
                close_tag = f"<!-- /{name}:{branch} -->"
                opened, closed = prompt.count(open_tag), prompt.count(close_tag)
                if opened != closed:
                    raise ValueError(
                        f"unbalanced {name}:{branch} markers in the prompt "
                        f"({opened} open, {closed} close)"
                    )
                if branch == keep:
                    prompt = prompt.replace(open_tag, "").replace(close_tag, "")
                else:
                    prompt = re.sub(
                        re.escape(open_tag) + ".*?" + re.escape(close_tag),
                        "",
                        prompt,
                        flags=re.DOTALL,
                    )
        # Dropping a block span leaves the blank lines that surrounded it.
        return re.sub(r"\n{3,}", "\n\n", prompt).strip()

    def _capabilities(self) -> dict[str, bool]:
        """What this session has, keyed by conditional name.

        One entry per name in :attr:`_CONDITIONALS`; adding a capability
        is a line here and a pair of markers in the markdown.
        """
        return {
            # agno attaches ``update_user_memory`` only when the session
            # has a LearningMachine *and* ``learning.user_memory`` is on,
            # so ``learning.enabled=false``, ``user_memory=false`` and any
            # session built without a db were all told to call a tool that
            # was not in their manifest.
            "user-memory": self._user_memory_available,
            # Derived: somewhere to persist *to*. The "when (not) to
            # persist" rules only mean anything next to a surface that
            # could take the fact; with neither they are a list of
            # things not to do with a tool nobody has. An extension
            # that keeps the project's documents says when to file
            # something in the ``when_to_persist`` slot, so a filled
            # slot is somewhere to persist to.
            "can-persist": "when_to_persist" in self._fills or self._user_memory_available,
        }

    def append_skills(self, prompt: str) -> str:
        """Append skill descriptions when auto-trigger is on.

        The heading used to read "Available Skills (user can invoke via /name)",
        which described the capability as something the *user* does. Nothing
        told the agent it could invoke a skill itself, by what means, or that it
        should prefer one when a request matches — so the agent mostly
        reimplemented the workflow by hand. Measured over capture sessions
        against prompts written to exercise exactly these six skills, it reached
        for one in 2 of 9 runs and otherwise went straight to shell.

        That is a real behavioural gap and not only a data-collection one: SOW
        4d expects ``/commit`` to be used rather than the work being improvised,
        and a skill is where a team's agreed procedure lives.
        """
        if not self._skill_descriptions:
            return prompt
        if not self._settings.skills.auto_trigger:
            return prompt
        return (
            prompt
            + "\n\n## Available Skills\n\n"
            + "Workflows this project has already codified. **You can invoke "
            + "these yourself** with the `slash_command` tool — "
            + '`slash_command("/commit")` — and it returns the skill\'s '
            + "instructions for you to carry out. The user can also type "
            + "`/name` directly.\n\n"
            + "**Prefer a skill over improvising.** When a request matches one "
            + "of these, invoke it instead of assembling the steps yourself: "
            + "the skill encodes the agreed procedure, and doing it by hand "
            + "quietly diverges from it. If the skill turns out not to fit, say "
            + "so and continue.\n"
            + self._skill_descriptions
        )

    def render(self) -> str:
        """Compose the full prompt.

        Sections are replaced before anything is substituted, so an
        extension's section may use ``{{AGENT_CATALOG}}`` and slots as
        igni's own do. Slots fill after substitution, because
        ``{{PERSISTING}}`` brings slots of its own. Conditionals
        resolve last, so a section pulled in from its own file — or
        written by an extension — has its spans resolved too.
        """
        prompt = self.load_base()
        prompt = self.replace_sections(prompt)
        prompt = self.substitute_placeholders(prompt)
        prompt = self.fill_slots(prompt)
        prompt = self.resolve_conditionals(prompt)
        prompt = self.append_skills(prompt)
        return prompt
