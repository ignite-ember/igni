"""igni ships its own agents, and only those.

The ``data-architect`` — the Cypher author — is CodeIndex's: it ships
in the extension's plugin and stands in only while its index covers
HEAD. Its contract is tested there. What is checked here is that igni
carries no extension's variants, and that the editing copies of igni's
own agents have not drifted from what ships.
"""

from __future__ import annotations

from pathlib import Path

#: What ships — ``src/ember_code/bundled_agents/`` is what the wheel carries and
#: what a user's session loads. The repo-root ``agents/`` directory is the
#: editing copy, and it is asserted identical so the two cannot drift.
AGENTS_DIR = Path(__file__).resolve().parent.parent / "src" / "ember_code" / "bundled_agents"
REPO_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


class TestTheShippedCopyIsTheOneTested:
    def test_core_ships_no_extensions_agents(self):
        """An extension's versions of igni's agents come with the extension.
        A copy here would load whether or not the extension is installed."""
        variants = sorted(p.name for p in AGENTS_DIR.glob("*.*.md"))
        assert not variants, f"core ships extension variants: {variants}"

    def test_no_editing_copy_has_drifted(self):
        """Pinning one file left the other sixteen free to rot, and they did.

        Every ``agents/*.md`` with a bundled counterpart had drifted: all of
        them still said ``grep_files`` / ``glob_files`` — retired in
        ``0bdfc28`` — and ``ember.md`` / ``ember-code``, from before the rename.
        Nothing is loaded from this directory (the loader reads
        ``project_dir/.igni/agents``), so none of it reached a session; but
        ``scripts/run_evals_smoke.py`` and
        ``scripts/_repro_explorer_no_tool_calls.py`` read ``agents/editor.md``
        and ``agents/explorer.md`` directly, and were measuring prompts that
        teach tools the registry raises on.

        Files that exist only in ``bundled_agents/`` are not required here —
        the editing copy is allowed to be a subset. It is not allowed to
        disagree.
        """
        drifted = [
            shipped.name
            for shipped in sorted(AGENTS_DIR.glob("*.md"))
            if (REPO_AGENTS_DIR / shipped.name).is_file()
            and (REPO_AGENTS_DIR / shipped.name).read_text() != shipped.read_text()
        ]
        assert not drifted, (
            f"{len(drifted)} editing copies differ from what ships: {drifted}. "
            "The bundled tree is authoritative — copy it over, don't edit here."
        )
