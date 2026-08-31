"""``plan_researcher`` agent definition contract.

The agent's prompt body tells it to research the codebase via
``codeindex_query`` / ``codeindex_tree`` (when CodeIndex is loaded)
or ``grep`` / ``find`` / ``cat`` / ``list_dir`` (fallback). For
those instructions to actually work, the agent's frontmatter
``tools:`` list must INCLUDE the toolkits the prompt asks for —
otherwise the sub-agent is spawned with only the tools it
declares (``WebFetch`` / ``WebSearch`` alone, in the original
shipped version) and every ``grep`` / ``cat`` call returns "tool
does not exist or is not available."

This test pins both variants so a future contributor who
trims the ``tools:`` list doesn't silently re-break plan-mode
research. Concrete regression: 2026-06-30 row-50 walkthrough
where the user typed ``/plan``, the main agent called
``enter_plan_mode(task=...)``, the researcher spawned, and
then sat there saying *"All tools are returning errors. ...
This appears to be a system issue."*
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Agent definitions live in ``src/ember_code/bundled_agents/`` —
# that's the shipped location (pip package includes them, git
# tracks them). The runtime copies them into a user's
# ``<project>/.igni/agents/`` on first launch via
# ``core.init.sync_bundled_content``. Tests read from the source
# so they don't depend on the gitignored ``.igni/`` staging dir.
PROJECT_AGENT_DIR = Path(__file__).resolve().parent.parent / "src" / "ember_code" / "bundled_agents"


def _parse_frontmatter(path: Path) -> dict:
    """Read a Markdown file with YAML frontmatter and return the
    parsed dict. Raises if the file doesn't start with ``---``."""
    text = path.read_text()
    if not text.startswith("---\n"):
        raise AssertionError(f"{path} missing YAML frontmatter")
    _, fm, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(fm) or {}
    if not isinstance(parsed, dict):
        raise AssertionError(f"{path} frontmatter is not a dict")
    return parsed


def _declared_tools(path: Path) -> set[str]:
    fm = _parse_frontmatter(path)
    raw = fm.get("tools", "")
    items = raw if isinstance(raw, list) else [t.strip() for t in str(raw).split(",") if t.strip()]
    return {str(t).strip() for t in items}


class TestPlanResearcherFallbackVariant:
    """``plan_researcher.md`` — used when CodeIndex is NOT available.
    The prompt body tells the agent to use grep / find / cat /
    list_dir, so the toolkit must expose those routes."""

    AGENT_FILE = PROJECT_AGENT_DIR / "plan_researcher.md"

    def test_agent_file_exists(self):
        assert self.AGENT_FILE.exists(), (
            "plan_researcher.md missing — ``enter_plan_mode`` "
            "falls back to manual research without it"
        )

    def test_declares_shell_for_grep_find_cat(self):
        # The prompt prescribes ``grep``/``find``/``cat`` for the
        # fallback variant — those run through ``Bash``. Without
        # ``Bash`` in the tools list the agent has no way to search
        # the codebase.
        tools = _declared_tools(self.AGENT_FILE)
        assert "Bash" in tools, (
            f"plan_researcher (fallback) needs Bash for grep/find/cat; "
            f"declared tools: {sorted(tools)}"
        )

    def test_declares_read_grep_glob_for_dedicated_routes(self):
        # The prompt also references the dedicated ``Read`` / ``Grep``
        # / ``Glob`` / ``LS`` toolkits as the same-purpose options
        # (some models reach for them by name). Declare them
        # explicitly so the agent has both shells.
        tools = _declared_tools(self.AGENT_FILE)
        for required in ("Read", "Grep", "Glob", "LS"):
            assert required in tools, (
                f"plan_researcher (fallback) should declare {required} — "
                f"prompt mentions read-only file ops. Declared: "
                f"{sorted(tools)}"
            )

    def test_declares_web_tools_for_external_research(self):
        tools = _declared_tools(self.AGENT_FILE)
        assert "WebFetch" in tools and "WebSearch" in tools

    def test_does_NOT_declare_write_tools(self):
        # Read-only contract — the agent must not be able to mutate
        # state. Plan mode also blocks edits at the eval layer, but
        # belt-and-suspenders: don't even hand the agent the tools.
        tools = _declared_tools(self.AGENT_FILE)
        for forbidden in ("Edit", "Write", "NotebookEdit"):
            assert forbidden not in tools, (
                f"plan_researcher must stay read-only; {forbidden} declared"
            )


class TestPlanResearcherCodeIndexVariant:
    """``plan_researcher.codeindex.md`` — used when CodeIndex IS
    available. The prompt body tells the agent to use
    ``codeindex_cypher`` as the primary research seam (the typed
    ``codeindex_query`` / ``codeindex_tree`` surface was removed
    from ``CodeIndexTools`` — see ``tests/test_data_architect_agent.py``
    for the pin), backed by ``grep_files`` / ``glob_files`` /
    ``Bash`` for shell fallback."""

    AGENT_FILE = PROJECT_AGENT_DIR / "plan_researcher.codeindex.md"

    def test_agent_file_exists(self):
        assert self.AGENT_FILE.exists()

    def test_declares_codeindex(self):
        # The defining feature of this variant — without ``CodeIndex``
        # in tools, ``codeindex_cypher`` resolves to "tool does not
        # exist" and the agent silently falls back to nothing.
        tools = _declared_tools(self.AGENT_FILE)
        assert "CodeIndex" in tools, (
            f"plan_researcher.codeindex.md MUST declare CodeIndex; declared tools: {sorted(tools)}"
        )

    def test_declares_ember_code_shell_tools_for_follow_up(self):
        # CodeIndex finds candidate files/symbols; the ember-code
        # shell toolkits (``grep_files`` / ``glob_files``) cover
        # text search and path-shape queries during drill-down.
        # ``Read`` / ``Grep`` / ``Glob`` / ``LS`` are Claude-Code
        # names and do not exist as ember-code tools — the previous
        # pin was stale.
        tools = _declared_tools(self.AGENT_FILE)
        for required in ("grep_files", "glob_files"):
            assert required in tools, (
                f"codeindex variant should declare {required} for "
                f"follow-up shell search. Declared: {sorted(tools)}"
            )

    def test_declares_bash_as_fallback(self):
        # CodeIndex may not cover uncommitted recent changes —
        # the prompt explicitly names Bash as the fallback for
        # those (also covers file reads via ``cat`` / ``sed -n``).
        tools = _declared_tools(self.AGENT_FILE)
        assert "Bash" in tools

    def test_does_NOT_declare_write_tools(self):
        tools = _declared_tools(self.AGENT_FILE)
        for forbidden in ("Edit", "Write", "NotebookEdit"):
            assert forbidden not in tools


class TestBothVariantsAgree:
    """Cross-variant invariants: both researchers share the same
    role, name, and read-only contract."""

    def test_both_share_name(self):
        fallback = _parse_frontmatter(PROJECT_AGENT_DIR / "plan_researcher.md")
        codeindex = _parse_frontmatter(PROJECT_AGENT_DIR / "plan_researcher.codeindex.md")
        assert fallback["name"] == codeindex["name"] == "plan_researcher"

    def test_both_are_read_only_tagged(self):
        fallback = _parse_frontmatter(PROJECT_AGENT_DIR / "plan_researcher.md")
        codeindex = _parse_frontmatter(PROJECT_AGENT_DIR / "plan_researcher.codeindex.md")
        assert "read-only" in (fallback.get("tags") or [])
        assert "read-only" in (codeindex.get("tags") or [])

    def test_both_cannot_orchestrate(self):
        # The researcher must not spawn further sub-agents — it's a
        # leaf in the orchestration tree.
        fallback = _parse_frontmatter(PROJECT_AGENT_DIR / "plan_researcher.md")
        codeindex = _parse_frontmatter(PROJECT_AGENT_DIR / "plan_researcher.codeindex.md")
        assert fallback.get("can_orchestrate") is False
        assert codeindex.get("can_orchestrate") is False
