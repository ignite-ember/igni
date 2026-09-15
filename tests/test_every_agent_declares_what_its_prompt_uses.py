"""An agent must declare every toolkit its own prompt tells it to use.

A sub-agent is spawned with exactly the tools on its frontmatter
``tools:`` line. A prompt that says "use ``grep_files``" while the
frontmatter lists only ``Bash`` produces an agent that answers *"All
tools are returning errors. This appears to be a system issue."* —
the 2026-06-30 regression recorded in
``tests/test_plan_researcher_agent.py``, where the user typed
``/plan``, the researcher spawned, and sat there.

That file pinned the contract for one agent by **listing** the tools
it should declare, and the list went stale twice: first pinning
``Read`` / ``Grep`` / ``Glob`` / ``LS``, then ``grep_files`` /
``glob_files``, each time outliving the prompt it described. This is
the same contract stated as a rule, over all nineteen bundled agents
rather than one.

What it can and cannot see is worth being plain about. It reads the
prompt for names the tool catalog knows, which catches the failure
that actually happens — a prompt edited without its frontmatter. It
cannot tell whether a declared tool is one the agent needs.

It used to carry one exclusion: ``grep``, which was both a function on
``GrepSpec`` and the shell command several prompts prescribe by name,
so a mention could not be attributed to either. ``Grep`` has since
been removed from the registry along with ``Read`` / ``Glob`` / ``LS``
/ ``Python``, and with it the collision — every remaining catalog name
is distinctive enough that a mention means what it says.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_AGENT_DIR = Path(__file__).resolve().parent.parent / "src/ember_code/bundled_agents"


def agent_files() -> list[Path]:
    return sorted(_AGENT_DIR.glob("*.md"))


def declared_tools(path: Path) -> set[str]:
    """The frontmatter ``tools:`` list, as a set of registry names."""
    text = path.read_text()
    if not text.startswith("---"):
        return set()
    front = yaml.safe_load(text.split("---", 2)[1]) or {}
    raw = front.get("tools", "")
    items = raw if isinstance(raw, list) else [t.strip() for t in str(raw).split(",")]
    return {str(t).strip() for t in items if str(t).strip()}


def tools_the_prompt_asks_for(path: Path) -> set[str]:
    """Registry names the prompt body names and the frontmatter omits.

    Function names come from the catalog's own
    ``agno_function_names``, so a tool renamed there is a tool this
    follows. Whole-word matches only, and fenced code blocks are
    skipped — a prompt quoting shell output is not asking for a
    toolkit.
    """
    from ember_code.core.tools.tool_spec import ToolSpecCatalog

    body = re.sub(r"```.*?```", "", path.read_text().split("---", 2)[-1], flags=re.S)
    declared = declared_tools(path)
    catalog = ToolSpecCatalog.default()
    missing: set[str] = set()
    for function, registry_name in catalog.agno_to_registry.items():
        if re.search(rf"\b{re.escape(function)}\b", body) and registry_name not in declared:
            missing.add(registry_name)
    return missing


class TestTheScanMeasuredSomething:
    """Every check below is vacuous if the inputs came back empty."""

    def test_there_are_agents_to_check(self):
        assert len(agent_files()) >= 15

    def test_the_catalog_knows_function_names(self):
        from ember_code.core.tools.tool_spec import ToolSpecCatalog

        assert len(ToolSpecCatalog.default().agno_to_registry) >= 15

    def test_the_rule_can_fire(self):
        """A rule nothing can fail is not one. Synthesised rather than
        waited for: a prompt naming ``notebook_edit_cell`` with no
        ``NotebookEdit`` declared must be reported."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text("---\nname: probe\ntools: Bash\n---\n\nCall notebook_edit_cell.\n")
            assert tools_the_prompt_asks_for(probe) == {"NotebookEdit"}


class TestEveryAgentDeclaresWhatItUses:
    @pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.name)
    def test_the_prompt_asks_for_nothing_it_was_not_given(self, path: Path):
        missing = tools_the_prompt_asks_for(path)

        assert missing == set(), (
            f"{path.name}'s prompt tells the agent to use {sorted(missing)}, and its "
            f"frontmatter declares {sorted(declared_tools(path))}. The sub-agent gets "
            "exactly what it declares, so every one of those calls comes back "
            '"tool does not exist" and the agent reports a system failure.'
        )

    @pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.name)
    def test_every_declared_tool_is_a_real_registry_name(self, path: Path):
        """The other direction.

        A typo or a retired toolkit in ``tools:`` is silent — the
        registry skips what it cannot resolve — so the agent quietly
        launches with fewer tools than its author intended.
        """
        from ember_code.core.tools.tool_spec import ToolSpecCatalog

        valid = ToolSpecCatalog.default().registry_names_with_aliases
        unknown = sorted(t for t in declared_tools(path) if t not in valid)

        assert unknown == [], (
            f"{path.name} declares {unknown}, which the tool registry does not "
            f"know. Known names: {sorted(valid)}"
        )


class TestCreateAgentAdvertisesTheRealList:
    """``create_agent``'s docstring is a prompt, not a comment.

    Agno sends a tool's docstring to the model as its description, so
    the "Valid: …" line in ``OrchestrateTools.create_agent`` is what a
    model reads before choosing tools for an ephemeral specialist. It
    listed ``Read, Grep, Glob, LS, Python`` for as long as those were
    in the registry and nothing declared them — the one place still
    pointing models at toolkits no shipped agent used.

    Now that they are gone, a stale list here would be worse than
    stale: the model would name a tool ``normalize`` rejects, and the
    agent it was creating would come back invalid.
    """

    @staticmethod
    def _advertised() -> set[str]:
        import re

        from ember_code.core.tools.orchestrate import OrchestrateTools

        doc = OrchestrateTools.create_agent.__doc__ or ""
        line = re.search(r"Valid:\s*(.+?)\.\s", doc, re.S)
        assert line, "create_agent's docstring no longer has a 'Valid:' line"
        return {t.strip() for t in re.split(r"[,\s]+", line.group(1)) if t.strip()}

    def test_it_offers_exactly_what_an_ephemeral_agent_may_request(self):
        from ember_code.core.tools.tool_spec import ToolSpecCatalog

        advertised = self._advertised()
        allowed = set(ToolSpecCatalog.default().valid_ephemeral_names)

        assert advertised == allowed, (
            f"create_agent tells the model it may use {sorted(advertised - allowed)} "
            f"which the registry rejects, and omits {sorted(allowed - advertised)} "
            "which it would accept."
        )
