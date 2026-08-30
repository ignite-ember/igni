"""Everything a group can ship, and whether the loader finds it.

An org's configuration is only worth shipping if it arrives somewhere a
loader looks. Each kind gets two checks: the cache writes it at the
convention its loader expects, and that loader — pointed at the group
directory — actually picks it up.

The path conventions are the interesting half. A skill is a directory
with ``SKILL.md`` inside it, a workflow is an ES module, a tool is a
Python file, and a hook is not a file per entry at all but a merged
settings-shaped block. Getting one of those wrong writes a file nothing
reads, which looks exactly like a working sync.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=list(entries),
    )


def _entry(kind: str, name: str, content: str, **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=kind,
        entry_name=name,
        content=content,
        content_type=extra.pop("content_type", "markdown"),
        **extra,
    )


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(cache_dir=tmp_path / "group-policy")


class TestWhereEachKindLands:
    """The filename conventions the loaders expect."""

    def test_an_agent_is_a_markdown_file(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("agents", "reviewer", "---\nname: reviewer\n---\nBody.")))

        assert (cache.dir_for("agents") / "reviewer.md").is_file()

    def test_a_skill_is_a_directory_with_skill_md_in_it(self, cache: GroupPolicyCache):
        """Not ``skills/deploy.md`` — the loader walks directories and
        reads SKILL.md from each."""
        cache.materialize(_pack(_entry("skills", "deploy", "---\nname: deploy\n---\nSteps.")))

        assert (cache.dir_for("skills") / "deploy" / "SKILL.md").is_file()

    def test_a_command_is_a_markdown_file(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("commands", "ship", "---\ndescription: Ship it\n---\nDo it."))
        )

        assert (cache.dir_for("commands") / "ship.md").is_file()

    def test_a_workflow_is_an_es_module(self, cache: GroupPolicyCache):
        """`.mjs`, because it is run by Node, not read as text."""
        cache.materialize(
            _pack(
                _entry(
                    "workflows",
                    "review",
                    "export const meta = { name: 'review' }\n",
                    content_type="javascript",
                )
            )
        )

        assert (cache.dir_for("workflows") / "review.mjs").is_file()

    def test_a_rule_is_a_markdown_file(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("rules", "style", "---\npaths: ['**/*.py']\n---\nUse tabs."))
        )

        assert (cache.dir_for("rules") / "style.md").is_file()

    def test_an_output_style_is_a_markdown_file(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("output-styles", "terse", "---\nname: terse\n---\nBe brief."))
        )

        assert (cache.dir_for("output-styles") / "terse.md").is_file()

    def test_a_tool_is_a_python_file(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("tools", "lookup", "def lookup():\n    pass\n", content_type="python"))
        )

        assert (cache.dir_for("tools") / "lookup.py").is_file()

    def test_an_mcp_server_is_wrapped_in_its_envelope(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("mcps", "github", '{"command": "gh-mcp"}', content_type="json"))
        )

        blob = json.loads((cache.dir_for("mcps") / "github.json").read_text(encoding="utf-8"))
        assert blob["mcpServers"]["github"]["command"] == "gh-mcp"


class TestHooks:
    """The one kind that is not a file per entry: hooks are declarations
    keyed by event, read from a settings-shaped file."""

    def test_a_whole_hooks_block_is_merged(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(
                _entry(
                    "hooks",
                    "guard",
                    json.dumps(
                        {"hooks": {"PreToolUse": [{"type": "command", "command": "guard.sh"}]}}
                    ),
                    content_type="json",
                )
            )
        )

        blob = json.loads((cache.dir_for("hooks") / "settings.json").read_text(encoding="utf-8"))
        assert blob["hooks"]["PreToolUse"][0]["command"] == "guard.sh"

    def test_a_bare_declaration_names_its_event(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(
                _entry(
                    "hooks",
                    "guard",
                    json.dumps({"event": "PreToolUse", "type": "command", "command": "guard.sh"}),
                    content_type="json",
                )
            )
        )

        blob = json.loads((cache.dir_for("hooks") / "settings.json").read_text(encoding="utf-8"))
        assert blob["hooks"]["PreToolUse"][0]["command"] == "guard.sh"

    def test_two_hooks_on_one_event_both_survive(self, cache: GroupPolicyCache):
        """They share a file, so a second entry must not replace the
        first."""
        cache.materialize(
            _pack(
                _entry(
                    "hooks",
                    "a",
                    json.dumps({"event": "PreToolUse", "type": "command", "command": "a.sh"}),
                    content_type="json",
                ),
                _entry(
                    "hooks",
                    "b",
                    json.dumps({"event": "PreToolUse", "type": "command", "command": "b.sh"}),
                    content_type="json",
                ),
            )
        )

        blob = json.loads((cache.dir_for("hooks") / "settings.json").read_text(encoding="utf-8"))
        assert {h["command"] for h in blob["hooks"]["PreToolUse"]} == {"a.sh", "b.sh"}

    def test_one_that_names_no_event_is_skipped_not_crashed(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("hooks", "nameless", json.dumps({"command": "x.sh"}), content_type="json"))
        )

        assert not (cache.dir_for("hooks") / "settings.json").exists()

    def test_the_file_goes_when_the_last_hook_does(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(
                _entry(
                    "hooks",
                    "guard",
                    json.dumps({"event": "PreToolUse", "type": "command", "command": "guard.sh"}),
                    content_type="json",
                )
            )
        )
        cache.materialize(_pack())

        assert not (cache.dir_for("hooks") / "settings.json").exists()


class TestTheLoadersFindThem:
    """Writing the file is half of it."""

    def test_a_skill_loads(self, cache: GroupPolicyCache):
        from ember_code.core.skills.loader import SkillPool

        cache.materialize(
            _pack(
                _entry(
                    "skills",
                    "deploy",
                    "---\nname: deploy\ndescription: Ship it\n---\nSteps here.",
                )
            )
        )
        pool = SkillPool()
        pool.load_directory(cache.dir_for("skills"))

        assert pool.get("deploy") is not None

    def test_a_command_loads(self, cache: GroupPolicyCache, tmp_path: Path):
        from ember_code.core.utils.markdown_commands import MarkdownCommand

        cache.materialize(
            _pack(_entry("commands", "ship", "---\ndescription: Ship it\n---\nDo the thing."))
        )
        found = MarkdownCommand.discover(
            tmp_path / "proj",
            read_claude=False,
            group_dir=cache.dir_for("commands"),
        )

        assert "ship" in found

    def test_an_output_style_loads(self, cache: GroupPolicyCache, tmp_path: Path):
        from ember_code.core.output_styles.loader import discover_output_styles

        cache.materialize(
            _pack(
                _entry(
                    "output-styles", "terse", "---\nname: terse\ndescription: Brief\n---\nBe brief."
                )
            )
        )
        styles = discover_output_styles(
            tmp_path / "proj",
            read_claude=False,
            group_dir=cache.dir_for("output-styles"),
        )

        assert "terse" in styles

    def test_a_workflow_is_discovered(self, cache: GroupPolicyCache, tmp_path: Path):
        from ember_code.backend.workflow_runner import WorkflowDiscovery

        cache.materialize(
            _pack(
                _entry(
                    "workflows",
                    "review",
                    "export const meta = { name: 'review', description: 'r' }\n",
                    content_type="javascript",
                )
            )
        )
        discovery = WorkflowDiscovery(
            project_dir=tmp_path / "proj",
            group_dir=cache.dir_for("workflows"),
        )

        assert [p.stem for p in discovery._iter_paths()] == ["review"]

    def test_a_tool_is_scanned(self, cache: GroupPolicyCache, tmp_path: Path):
        from ember_code.core.tools.custom_loader import CustomToolLoader

        cache.materialize(
            _pack(_entry("tools", "noop", "# nothing to register\n", content_type="python"))
        )
        result = CustomToolLoader().discover(
            tmp_path / "proj",
            group_tools_dir=cache.dir_for("tools"),
        )

        # It defines no toolkit, so what matters is that the file was
        # read rather than skipped as a missing directory.
        assert not result.failed

    def test_a_hook_loads(self, cache: GroupPolicyCache, tmp_path: Path):
        from ember_code.core.hooks.loader import HookLoader

        cache.materialize(
            _pack(
                _entry(
                    "hooks",
                    "guard",
                    json.dumps(
                        {"hooks": {"PreToolUse": [{"type": "command", "command": "guard.sh"}]}}
                    ),
                    content_type="json",
                )
            )
        )
        result = HookLoader(
            tmp_path / "proj",
            cross_tool_support=False,
            group_dir=cache.dir_for("hooks"),
        ).load()

        assert result.registry.for_event("PreToolUse")

    def test_a_scoped_rule_is_indexed(self, cache: GroupPolicyCache, tmp_path: Path):
        from ember_code.core.utils.rules_index import RulesIndex

        cache.materialize(
            _pack(_entry("rules", "python", "---\npaths: ['**/*.py']\n---\nUse type hints."))
        )
        project = tmp_path / "proj"
        project.mkdir()
        index = RulesIndex(
            project,
            read_claude_md=False,
            group_rules_dir=cache.dir_for("rules"),
        )

        assert index.consume_path(project / "app" / "main.py")


class TestWhatTheGroupStopsShipping:
    def test_a_dropped_entry_is_removed(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("commands", "ship", "---\ndescription: d\n---\nGo.")))
        cache.materialize(_pack())

        assert not (cache.dir_for("commands") / "ship.md").exists()

    def test_a_dropped_skill_takes_its_directory(self, cache: GroupPolicyCache):
        cache.materialize(_pack(_entry("skills", "deploy", "---\nname: deploy\n---\nSteps.")))
        cache.materialize(_pack())

        assert not (cache.dir_for("skills") / "deploy").exists()

    def test_a_disabled_entry_is_not_written(self, cache: GroupPolicyCache):
        cache.materialize(
            _pack(_entry("commands", "ship", "---\ndescription: d\n---\nGo.", enabled=False))
        )

        assert not (cache.dir_for("commands") / "ship.md").exists()


def test_the_session_knows_where_each_kind_lives(tmp_path: Path):
    """One method answers it, so eight loaders cannot disagree."""
    from ember_code.core.session.core import Session

    session = Session.__new__(Session)
    session._group_policy_dir = tmp_path / "group-policy"

    assert session.group_dir_for("skills") == tmp_path / "group-policy" / "skills"
    assert session.group_dir_for("workflows") == tmp_path / "group-policy" / "workflows"
