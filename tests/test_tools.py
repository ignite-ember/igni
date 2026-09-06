"""Tests for tools — registry, edit, search."""

import pytest

from ember_code.core.tools.edit import EmberEditTools
from ember_code.core.tools.registry import ToolRegistry
from ember_code.core.tools.search import GlobTools


class TestToolRegistry:
    def test_available_tools(self):
        reg = ToolRegistry()
        names = reg.available_tools
        # ``Read`` / ``Grep`` / ``Glob`` / ``LS`` / ``Python`` were
        # removed from the registry — nothing shipped declared them and
        # the main agent never had them, so searching and reading go
        # through ``Bash``. Asserted as absent too: a registry that
        # quietly reinstates them is a registry handing agents tools
        # nobody decided to give back.
        assert "Write" in names
        assert "Edit" in names
        assert "Bash" in names
        assert "NotebookEdit" in names
        for gone in ("Read", "Grep", "Glob", "LS", "Python"):
            assert gone not in names

    def test_resolve_single_tool(self):
        reg = ToolRegistry()
        tools = reg.resolve(["Write"])
        assert len(tools) == 1

    def test_resolve_multiple_tools(self):
        reg = ToolRegistry()
        tools = reg.resolve(["Write", "Edit", "NotebookEdit"])
        assert len(tools) == 3

    def test_resolve_comma_string(self):
        reg = ToolRegistry()
        tools = reg.resolve("Write, Edit, NotebookEdit")
        assert len(tools) == 3

    def test_resolve_deduplicates_bash(self):
        reg = ToolRegistry()
        tools = reg.resolve(["Bash", "BashOutput"])
        assert len(tools) == 1

    def test_resolve_unknown_raises(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="Unknown tool"):
            reg.resolve(["FakeToolThatDoesNotExist"])

    def test_resolve_skips_mcp_and_orchestrate(self):
        reg = ToolRegistry()
        tools = reg.resolve(["Write", "MCP:github", "Orchestrate"])
        assert len(tools) == 1

    def test_register_custom_tool(self):
        reg = ToolRegistry()
        reg.register("Custom", lambda confirm=False: "custom_instance")
        tools = reg.resolve(["Custom"])
        assert tools == ["custom_instance"]

    def test_resolve_tools_direct(self):
        registry = ToolRegistry()
        tools = registry.resolve(["Write", "Edit"])
        assert len(tools) == 2


class TestEmberEditTools:
    def test_edit_file_single_match(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world\n")

        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.edit_file(str(f), "hello", "goodbye")
        assert "Successfully edited" in result
        assert f.read_text() == "goodbye world\n"

    def test_edit_file_not_found(self, tmp_path):
        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.edit_file(str(tmp_path / "missing.py"), "a", "b")
        assert "Error: File not found" in result

    def test_edit_file_no_match(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world\n")

        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.edit_file(str(f), "nonexistent", "replacement")
        assert "not found" in result

    def test_edit_file_multiple_matches(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa bbb aaa\n")

        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.edit_file(str(f), "aaa", "ccc")
        assert "appears 2 times" in result
        # File should be unchanged
        assert f.read_text() == "aaa bbb aaa\n"

    def test_edit_file_replace_all(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("aaa bbb aaa\n")

        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.edit_file_replace_all(str(f), "aaa", "ccc")
        assert "2 occurrence(s)" in result
        assert f.read_text() == "ccc bbb ccc\n"

    def test_create_file(self, tmp_path):
        editor = EmberEditTools(base_dir=str(tmp_path))
        target = tmp_path / "new_file.py"
        result = editor.create_file(str(target), "print('hello')\n")
        assert "Successfully created" in result
        assert target.read_text() == "print('hello')\n"

    def test_create_file_already_exists(self, tmp_path):
        f = tmp_path / "existing.py"
        f.write_text("old content")

        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.create_file(str(f), "new content")
        assert "already exists" in result
        assert f.read_text() == "old content"

    def test_create_file_nested_dirs(self, tmp_path):
        editor = EmberEditTools(base_dir=str(tmp_path))
        target = tmp_path / "a" / "b" / "c.py"
        result = editor.create_file(str(target), "nested\n")
        assert "Successfully created" in result
        assert target.read_text() == "nested\n"

    def test_relative_path_resolution(self, tmp_path):
        f = tmp_path / "rel.py"
        f.write_text("content\n")

        editor = EmberEditTools(base_dir=str(tmp_path))
        result = editor.edit_file("rel.py", "content", "replaced")
        assert "Successfully edited" in result


class TestGlobTools:
    def test_glob_files(self, tmp_path):
        (tmp_path / "a.py").write_text("a")
        (tmp_path / "b.py").write_text("b")
        (tmp_path / "c.txt").write_text("c")

        glob_tool = GlobTools(base_dir=str(tmp_path))
        result = glob_tool.glob_files("*.py")
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_glob_no_matches(self, tmp_path):
        glob_tool = GlobTools(base_dir=str(tmp_path))
        result = glob_tool.glob_files("*.xyz")
        assert "No files matching" in result

    def test_glob_nonexistent_dir(self, tmp_path):
        glob_tool = GlobTools(base_dir=str(tmp_path))
        result = glob_tool.glob_files("*.py", path="nonexistent")
        assert "Error" in result

    def test_glob_max_results(self, tmp_path):
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text(str(i))

        glob_tool = GlobTools(base_dir=str(tmp_path))
        result = glob_tool.glob_files("*.py", max_results=3)
        lines = [line for line in result.strip().splitlines() if line]
        assert len(lines) == 3

    def test_glob_subdirectory(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("main")
        (tmp_path / "root.py").write_text("root")

        glob_tool = GlobTools(base_dir=str(tmp_path))
        result = glob_tool.glob_files("*.py", path="src")
        assert "main.py" in result
        assert "root.py" not in result
