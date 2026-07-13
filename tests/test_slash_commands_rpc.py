"""Tests for the ``get_slash_commands`` RPC — the SDK-style
enumeration that mirrors Claude Code's ``slash_commands`` field.

Validates three sources contribute (builtin / markdown / skill),
each entry carries the expected shape, and the
``cross_tool_support`` toggle is honoured for markdown discovery.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_code.backend.__main__ import _build_rpc_table
from ember_code.backend.server import BackendServer
from ember_code.core.skills.parser import SkillDefinition
from ember_code.protocol.rpc import RpcMethod


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _make_backend(project_dir: Path, *, skills: list | None = None, cross_tool: bool = True):
    """Build a thin ``BackendServer`` stand-in with just enough
    attributes for ``get_slash_commands`` to walk all three
    sources without bootstrapping a real session."""
    session = MagicMock()
    session.project_dir = project_dir
    session.settings.rules.cross_tool_support = cross_tool
    session.skill_pool.list_skills.return_value = skills or []
    backend = BackendServer.__new__(BackendServer)
    backend._session = session
    return backend


class TestGetSlashCommands:
    def test_returns_builtins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        backend = _make_backend(tmp_path)
        out = backend.get_slash_commands()
        names = {entry["name"] for entry in out if entry["source"] == "builtin"}
        # Sanity: well-known built-ins all present (and unprefixed —
        # the bare name lets the caller render its own slash).
        assert {"help", "clear", "compact", "model", "agents", "skills"} <= names

    def test_builtins_have_descriptions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        backend = _make_backend(tmp_path)
        out = backend.get_slash_commands()
        help_entry = next(e for e in out if e["name"] == "help")
        assert help_entry["description"]  # non-empty
        assert help_entry["source"] == "builtin"
        assert help_entry["argument_hint"] == ""

    def test_bypass_is_listed_with_description(self, tmp_path, monkeypatch):
        """``/bypass`` (the footer auto-approve switch's BE half)
        must show up in the catalog with a description so the
        slash-popup can surface it to users who'd rather type
        than reach for the switch."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        backend = _make_backend(tmp_path)
        out = backend.get_slash_commands()
        bypass = next(
            (e for e in out if e["name"] == "bypass" and e["source"] == "builtin"),
            None,
        )
        assert bypass is not None, "/bypass missing from slash command catalog"
        assert bypass["description"]  # non-empty

    def test_includes_markdown_commands(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(
            tmp_path / ".ember" / "commands" / "review.md",
            "---\ndescription: Review the diff\nargument-hint: <path>\n---\nBody\n",
        )
        backend = _make_backend(tmp_path)
        out = backend.get_slash_commands()
        review = next(e for e in out if e["source"] == "markdown" and e["name"] == "review")
        assert review["description"] == "Review the diff"
        assert review["argument_hint"] == "<path>"

    def test_markdown_discovery_respects_cross_tool_toggle(self, tmp_path, monkeypatch):
        """When cross_tool_support is off, a ``.claude/commands/``
        drop-in must not surface — the toggle is the user's
        consent for cross-tool reads."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(home / ".claude" / "commands" / "claude_only.md", "Body\n")
        _write(tmp_path / ".ember" / "commands" / "ember_only.md", "Body\n")

        backend = _make_backend(tmp_path, cross_tool=False)
        out = backend.get_slash_commands()
        markdown_names = {e["name"] for e in out if e["source"] == "markdown"}
        assert "claude_only" not in markdown_names
        assert "ember_only" in markdown_names

    def test_includes_user_invocable_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        skills = [
            SkillDefinition(
                name="planner",
                description="Plan a task",
                argument_hint="<topic>",
                user_invocable=True,
            ),
        ]
        backend = _make_backend(tmp_path, skills=skills)
        out = backend.get_slash_commands()
        planner = next(e for e in out if e["source"] == "skill")
        assert planner["name"] == "planner"
        assert planner["description"] == "Plan a task"
        assert planner["argument_hint"] == "<topic>"

    def test_excludes_non_user_invocable_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        skills = [
            SkillDefinition(name="internal", description="hidden", user_invocable=False),
        ]
        backend = _make_backend(tmp_path, skills=skills)
        out = backend.get_slash_commands()
        skill_names = {e["name"] for e in out if e["source"] == "skill"}
        assert "internal" not in skill_names

    def test_all_three_sources_in_one_response(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".ember" / "commands" / "mdone.md", "Body\n")
        skills = [SkillDefinition(name="skillone", description="s", user_invocable=True)]
        backend = _make_backend(tmp_path, skills=skills)
        out = backend.get_slash_commands()
        sources = {e["source"] for e in out}
        assert sources == {"builtin", "markdown", "skill"}

    def test_entry_shape_consistent(self, tmp_path, monkeypatch):
        """Every entry has the same four keys with the same types
        regardless of source. Lets SDK consumers iterate without
        per-source branching."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        _write(tmp_path / ".ember" / "commands" / "x.md", "Body\n")
        skills = [SkillDefinition(name="y", description="d", user_invocable=True)]
        backend = _make_backend(tmp_path, skills=skills)
        out = backend.get_slash_commands()
        assert out  # non-empty
        for entry in out:
            assert set(entry.keys()) == {"name", "description", "source", "argument_hint"}
            assert isinstance(entry["name"], str)
            assert isinstance(entry["description"], str)
            assert entry["source"] in ("builtin", "markdown", "skill")
            assert isinstance(entry["argument_hint"], str)
            # The leading slash is the CALLER's job to add.
            assert not entry["name"].startswith("/")

    def test_skill_enumeration_failure_does_not_sink_call(self, tmp_path, monkeypatch):
        """If skill enumeration raises (e.g. a misconfigured
        pool), the RPC should still return built-ins + markdown
        — partial degradation beats a hard failure for an
        enumeration endpoint."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        _write(tmp_path / ".ember" / "commands" / "mdone.md", "Body\n")

        session = MagicMock()
        session.project_dir = tmp_path
        session.settings.rules.cross_tool_support = True
        session.skill_pool.list_skills.side_effect = RuntimeError("boom")
        backend = BackendServer.__new__(BackendServer)
        backend._session = session

        out = backend.get_slash_commands()
        sources = {e["source"] for e in out}
        assert "builtin" in sources
        assert "markdown" in sources
        assert "skill" not in sources


class TestRpcIntegration:
    """The enum value and dispatch table must agree — catches the
    "added enum member, forgot to register" mistake at test time
    instead of waiting for the live ``validate_rpc_table`` call
    at server startup."""

    def test_get_slash_commands_enum_value(self):
        assert RpcMethod.GET_SLASH_COMMANDS.value == "get_slash_commands"

    @pytest.mark.asyncio
    async def test_dispatch_table_registers_handler(self, tmp_path, monkeypatch):
        """Build the actual RPC dispatch table and verify the
        ``get_slash_commands`` entry calls through to the backend
        method we wrote. Stops a future refactor from breaking
        the wire-level contract silently."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        backend = _make_backend(tmp_path)
        table = _build_rpc_table(backend, transport=MagicMock(), login_state={})
        handler = table.get(RpcMethod.GET_SLASH_COMMANDS)
        assert handler is not None
        result = handler({})
        # Dispatch entries are sync lambdas wrapping the backend
        # method; this one returns the list directly.
        assert isinstance(result, list)
        assert any(entry["source"] == "builtin" for entry in result)
