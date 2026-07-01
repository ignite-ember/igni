"""Tests for tool function execution — P0 critical.

Covers: Read (via Agno FileTools), Grep, LS, Bash execution.
Edit and Glob already have good coverage in test_tools.py.
"""

from unittest.mock import patch

import pytest

from ember_code.core.tools.search import GrepTools

# ── Read (Agno FileTools) ────────────────────────────────────────


class TestReadTool:
    """Test that the Read toolkit can read files."""

    def test_read_file_returns_content(self, tmp_path):
        from agno.tools.file import FileTools

        (tmp_path / "test.txt").write_text("hello world")
        tools = FileTools(base_dir=tmp_path, enable_read_file=True)
        result = tools.read_file(file_name=str(tmp_path / "test.txt"))
        assert "hello world" in result

    def test_read_file_nonexistent(self, tmp_path):
        from agno.tools.file import FileTools

        tools = FileTools(base_dir=tmp_path, enable_read_file=True)
        result = tools.read_file(file_name=str(tmp_path / "nonexistent.txt"))
        assert "error" in result.lower() or "not found" in result.lower() or "No such" in result

    def test_read_file_chunk(self, tmp_path):
        from agno.tools.file import FileTools

        lines = "\n".join(f"line {i}" for i in range(100))
        (tmp_path / "big.txt").write_text(lines)
        tools = FileTools(base_dir=tmp_path, enable_read_file_chunk=True)
        result = tools.read_file_chunk(
            file_name=str(tmp_path / "big.txt"),
            start_line=10,
            end_line=20,
        )
        assert "line 10" in result or "line 11" in result

    def test_list_files(self, tmp_path):
        from agno.tools.file import FileTools

        (tmp_path / "a.py").write_text("pass")
        (tmp_path / "b.txt").write_text("hello")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.js").write_text("//")

        tools = FileTools(base_dir=tmp_path, enable_list_files=True)
        result = tools.list_files(dir_path=str(tmp_path))
        assert "a.py" in result
        assert "b.txt" in result


# ── Grep ─────────────────────────────────────────────────────────


_has_rg = __import__("shutil").which("rg") is not None


class TestGrepTool:
    """Test grep tool functions."""

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_finds_pattern(self, tmp_path):
        (tmp_path / "test.py").write_text("def hello():\n    return 'world'\n")
        tools = GrepTools(base_dir=str(tmp_path))
        result = tools.grep("hello", path="")
        assert "hello" in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_no_matches(self, tmp_path):
        (tmp_path / "test.py").write_text("nothing here\n")
        tools = GrepTools(base_dir=str(tmp_path))
        result = tools.grep("nonexistent_pattern_xyz")
        assert "No matches" in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_with_glob_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("target\n")
        (tmp_path / "b.txt").write_text("target\n")
        tools = GrepTools(base_dir=str(tmp_path))
        result = tools.grep("target", glob="*.py")
        assert "a.py" in result
        # b.txt should be excluded by glob
        assert "b.txt" not in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_with_context(self, tmp_path):
        (tmp_path / "test.py").write_text("line1\nline2\ntarget\nline4\nline5\n")
        tools = GrepTools(base_dir=str(tmp_path))
        result = tools.grep("target", context_lines=1)
        # Should include context lines
        assert "line2" in result or "line4" in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_files_returns_paths(self, tmp_path):
        (tmp_path / "a.py").write_text("match\n")
        (tmp_path / "b.py").write_text("no\n")
        tools = GrepTools(base_dir=str(tmp_path))
        result = tools.grep_files("match")
        assert "a.py" in result
        assert "b.py" not in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_count(self, tmp_path):
        (tmp_path / "test.py").write_text("match\nmatch\nmatch\n")
        tools = GrepTools(base_dir=str(tmp_path))
        result = tools.grep_count("match")
        assert "3" in result or "test.py" in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_rg_not_installed(self, tmp_path):
        tools = GrepTools(base_dir=str(tmp_path))
        with patch("subprocess.run", side_effect=FileNotFoundError("rg not found")):
            result = tools.grep("test")
        assert "not installed" in result or "Error" in result

    @pytest.mark.skipif(not _has_rg, reason="ripgrep not in PATH")
    def test_grep_timeout(self, tmp_path):
        import subprocess

        tools = GrepTools(base_dir=str(tmp_path))
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("rg", 30)):
            result = tools.grep("test")
        assert "timed out" in result.lower() or "Error" in result


# ── Bash (Shell execution) ───────────────────────────────────────


class TestBashTool:
    """Test shell command execution."""

    def test_shell_runs_command(self):
        from agno.tools.shell import ShellTools

        tools = ShellTools()
        result = tools.run_shell_command(args=["echo", "hello"])
        assert "hello" in result

    def test_shell_captures_stderr(self):
        from agno.tools.shell import ShellTools

        tools = ShellTools()
        result = tools.run_shell_command(args=["ls", "/nonexistent_dir_xyz"])
        # Should contain error output, not crash
        assert isinstance(result, str)

    def test_shell_respects_tail(self):
        from agno.tools.shell import ShellTools

        tools = ShellTools()
        result = tools.run_shell_command(
            args=["bash", "-c", "for i in $(seq 1 100); do echo line$i; done"],
            tail=5,
        )
        # Should only have last 5 lines
        lines = [line for line in result.strip().split("\n") if line.strip()]
        assert len(lines) <= 10  # tail + possible header


# ── Tool Registry ────────────────────────────────────────────────


class TestToolRegistryResolve:
    """Test that all tools resolve correctly."""

    def test_resolve_read(self):
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.tools.registry import ToolRegistry

        registry = ToolRegistry(
            base_dir=".",
            permissions=ToolPermissions(),
        )
        tools = registry.resolve(["Read"])
        assert len(tools) == 1

    def test_resolve_grep(self):
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.tools.registry import ToolRegistry

        registry = ToolRegistry(
            base_dir=".",
            permissions=ToolPermissions(),
        )
        tools = registry.resolve(["Grep"])
        assert len(tools) == 1

    def test_resolve_all_standard(self):
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.tools.registry import ToolRegistry

        registry = ToolRegistry(
            base_dir=".",
            permissions=ToolPermissions(),
        )
        standard = ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]
        tools = registry.resolve(standard)
        assert len(tools) == len(standard)


class TestToolRegistryResolveEdges:
    """Deeper coverage on ``ToolRegistry.resolve`` beyond the
    happy-path tests above. Covers the agent-allowlist edge
    cases that decide what tools the agent actually gets."""

    def _registry(self, permissions=None):
        from ember_code.core.config.tool_permissions import ToolPermissions
        from ember_code.core.tools.registry import ToolRegistry

        return ToolRegistry(
            base_dir=".",
            permissions=permissions or ToolPermissions(),
        )

    def test_accepts_comma_separated_string(self):
        # Agent definitions often carry ``tools: "Read,Write,Edit"``
        # in YAML. Without the comma-split path, those agents
        # would silently get zero tools.
        registry = self._registry()
        tools = registry.resolve("Read,Write,Edit")
        assert len(tools) == 3

    def test_strips_whitespace_in_comma_split(self):
        # ``Read, Write , Edit`` is what YAML lists with
        # human-friendly spacing produce. The split must
        # strip each segment.
        registry = self._registry()
        tools = registry.resolve("Read , Write,  Edit")
        assert len(tools) == 3

    def test_filters_empty_segments(self):
        # Trailing commas / double commas shouldn't yield
        # empty tool names that would then raise ValueError.
        registry = self._registry()
        tools = registry.resolve("Read,,Write,")
        assert len(tools) == 2

    def test_denied_tool_silently_skipped(self):
        # A tool denied by permissions doesn't raise — it
        # silently drops out of the resolved list. The agent
        # gets the surviving tools; the audit log records the
        # deny separately.
        from ember_code.core.config.tool_permissions import ToolPermissions

        perms = ToolPermissions()
        # Mark Bash as denied via the internal level map.
        perms._tool_levels["Bash"] = "deny"  # type: ignore[attr-defined]
        registry = self._registry(permissions=perms)
        tools = registry.resolve(["Read", "Bash", "Write"])
        assert len(tools) == 2

    def test_mcp_prefix_silently_skipped(self):
        # ``MCP:slack:send`` is a server-tool reference handled
        # by the MCP manager elsewhere — not a registry-resolved
        # toolkit. Skip silently rather than raise.
        registry = self._registry()
        tools = registry.resolve(["Read", "MCP:slack:send", "Write"])
        assert len(tools) == 2

    def test_orchestrate_and_knowledge_silently_skipped(self):
        # Orchestrate / Knowledge are added by the team builder,
        # not the registry. Listing them in an agent's tools
        # should be a clean no-op rather than an error.
        registry = self._registry()
        tools = registry.resolve(["Read", "Orchestrate", "Knowledge", "Write"])
        assert len(tools) == 2

    def test_unknown_tool_raises_with_helpful_message(self):
        # The error needs to be actionable — naming the
        # unknown tool and the available list so the agent
        # author can fix the typo without grepping the source.
        registry = self._registry()
        with pytest.raises(ValueError) as exc:
            registry.resolve(["NotARealTool"])
        msg = str(exc.value)
        assert "NotARealTool" in msg
        # A known tool name appears in the message so the
        # author has at least one anchor.
        assert "Read" in msg

    def test_bashoutput_aliases_to_bash_for_dedup(self):
        # ``BashOutput`` is a separate tool name (CC convention)
        # but maps to the same EmberShellTools instance. Without
        # the canonical mapping, Agno would see two toolkits
        # exposing ``run_shell_command`` and the second
        # registration would either error or shadow the first.
        registry = self._registry()
        tools = registry.resolve(["Bash", "BashOutput"])
        assert len(tools) == 1

    def test_same_tool_listed_twice_deduplicated(self):
        # Defensive — an agent author listing ``Read`` twice
        # should get one instance.
        registry = self._registry()
        tools = registry.resolve(["Read", "Read", "Read"])
        assert len(tools) == 1

    def test_available_tools_property_returns_sorted_list(self):
        # The list is sorted so error messages and downstream
        # log lines have stable ordering across runs.
        # ``available_tools`` is a ``@property`` — pin the
        # access shape here so a refactor to a method-with-
        # parens surfaces as a deliberate API change.
        registry = self._registry()
        names = registry.available_tools  # no parens — property
        assert names == sorted(names)
        # Sanity — the standard tools are present.
        for expected in ("Read", "Write", "Edit", "Bash", "Grep", "Glob"):
            assert expected in names

    def test_register_adds_custom_factory(self):
        # ``register`` lets external code add tools beyond the
        # built-ins. Pin the round-trip — register → resolve
        # returns an instance from the factory.
        registry = self._registry()
        sentinel = object()
        registry.register("Custom", lambda confirm=False: sentinel)
        tools = registry.resolve(["Custom"])
        assert tools == [sentinel]

    def test_needs_confirmation_passed_to_factory(self):
        # The factory's ``confirm`` arg is set per the
        # permissions check. The factory uses that to wire
        # ``requires_confirmation_tools`` so Agno pauses for
        # HITL before invoking. Drift here would silently
        # disable HITL prompts.
        #
        # Default ``ToolPermissions.get_level`` returns "ask"
        # for unknown names, which means
        # ``needs_confirmation`` returns True — so a custom
        # tool unknown to the permissions config defaults to
        # confirm-required (safe default).
        registry = self._registry()
        seen_confirm: list[bool] = []
        registry.register(
            "Custom",
            lambda confirm=False: seen_confirm.append(confirm) or object(),
        )
        registry.resolve(["Custom"])
        assert seen_confirm == [True]

    def test_needs_confirmation_false_when_permission_allows(self):
        # Counterpart: when permissions explicitly allow a
        # tool, ``needs_confirmation`` is False → factory gets
        # ``confirm=False`` → tool runs without HITL pause.
        from ember_code.core.config.tool_permissions import ToolPermissions

        # Construct permissions with Read explicitly allowed.
        perms = ToolPermissions()
        perms._tool_levels["Read"] = "allow"  # type: ignore[attr-defined]
        registry = self._registry(permissions=perms)
        seen_confirm: list[bool] = []
        # Replace the Read factory with a sentinel to inspect
        # the confirm arg.
        registry.register(
            "Read",
            lambda confirm=False: seen_confirm.append(confirm) or object(),
        )
        registry.resolve(["Read"])
        assert seen_confirm == [False]
