"""System-prompt assembly stage.

Owns the base system prompt loaded from ``core/prompts/``: picks
the CodeIndex variant when available, substitutes the ``{{AGENT_
CATALOG}}`` placeholder from the agent pool's catalog, appends the
per-commit Project Map (best-effort — missing map is non-fatal),
and tacks on the skill-descriptions block when skills are
configured to auto-trigger.

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

from ember_code.core.code_index.manifest import Manifest
from ember_code.core.code_index.project_map import ProjectMap

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
    #: capability switchable.
    _CONDITIONALS: ClassVar[tuple[str, ...]] = ("artifacts", "codeindex")

    def __init__(
        self,
        *,
        project_dir: Path,
        settings: Any,
        codeindex_available: bool,
        agent_catalog: str,
        skill_descriptions: str,
        prompt_loader: Callable[[str], str],
        artifacts_available: bool = False,
    ) -> None:
        self._project_dir = project_dir
        self._settings = settings
        self._codeindex_available = codeindex_available
        self._artifacts_available = artifacts_available
        self._agent_catalog = agent_catalog
        self._skill_descriptions = skill_descriptions
        self._prompt_loader = prompt_loader

    def load_base(self) -> str:
        """Pick + load the base template.

        When CodeIndex is available we load
        ``main_agent.codeindex.md`` — a wholly CodeIndex-first
        variant with the tool reference inline and read-before-
        edit / search guidance re-framed around the index. The
        ``{{CODEINDEX_TOOLS}}`` placeholder only exists in the
        plain variant; for the codeindex variant the replacement
        is a no-op.
        """
        name = "main_agent.codeindex" if self._codeindex_available else "main_agent"
        return self._prompt_loader(name)

    def substitute_placeholders(self, prompt: str) -> str:
        """Substitute ``{{AGENT_CATALOG}}``, ``{{CODEINDEX_TOOLS}}``,
        ``{{PERSISTING}}``."""
        catalog = self._agent_catalog or "(no agents loaded)"
        prompt = prompt.replace("{{AGENT_CATALOG}}", catalog)
        prompt = prompt.replace("{{CODEINDEX_TOOLS}}", "")
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
        exactly one of them shown. ``<!-- artifacts:only -->`` survives
        when the store is up, ``<!-- artifacts:absent -->`` when it is
        not, and the markers themselves never reach the model.

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
        for name in self._CONDITIONALS:
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
            "artifacts": self._artifacts_available,
            "codeindex": self._codeindex_available,
        }

    def append_project_map(self, prompt: str) -> str:
        """Append the per-commit Project Map (best-effort).

        Auto-generated by ``apply_delta``; gives the agent a
        factual overview (taxonomy, tables, cached-resource
        wrappers, vocabulary glossary, entry points) before it
        issues a single tool call. Missing map is non-fatal — the
        prompt still works without it.
        """
        if not self._codeindex_available:
            return prompt
        try:
            manifest = Manifest(
                project=self._project_dir,
                data_dir=self._settings.storage.data_dir,
            )
            head_sha = manifest.load().head
            if not head_sha:
                return prompt
            map_md = ProjectMap(
                self._project_dir,
                head_sha,
                data_dir=self._settings.storage.data_dir,
            ).load()
            if map_md:
                return prompt + "\n\n## Project Map\n\n" + map_md
        except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
            # Missing / unreadable manifest / bad JSON — project map
            # is optional, degrade silently. Anything else (AttributeError,
            # TypeError, etc.) is a programming bug and should surface.
            logger.debug("project-map append failed: %s", exc)
        return prompt

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
        """Compose the full prompt through the five phases.

        Conditionals resolve *after* substitution, so a section pulled
        in from its own file has its spans resolved too — otherwise a
        block would behave differently depending on whether it happened
        to be inlined or kept beside the prompt.
        """
        prompt = self.load_base()
        prompt = self.substitute_placeholders(prompt)
        prompt = self.resolve_conditionals(prompt)
        prompt = self.append_project_map(prompt)
        prompt = self.append_skills(prompt)
        return prompt
