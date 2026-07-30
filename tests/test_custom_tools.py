"""Tests for custom tool loader — .ember/tools/ discovery."""

import textwrap
from pathlib import Path

import pytest
from agno.tools import tool

from ember_code.core.tools.custom_loader import CustomToolkit, load_custom_tools


@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    """Create a .ember/tools/ directory inside a temp project."""
    d = tmp_path / ".ember" / "tools"
    d.mkdir(parents=True)
    return d


def _write_tool_file(tools_dir: Path, filename: str, content: str) -> Path:
    p = tools_dir / filename
    p.write_text(textwrap.dedent(content))
    return p


class TestLoadCustomTools:
    def test_empty_dir(self, tmp_path: Path):
        """No .ember/tools/ directory — returns empty list."""
        toolkits = load_custom_tools(tmp_path)
        assert toolkits == []

    def test_empty_tools_dir(self, tools_dir: Path, tmp_path: Path):
        """Empty .ember/tools/ — returns empty list."""
        toolkits = load_custom_tools(tmp_path)
        assert toolkits == []

    def test_single_tool(self, tools_dir: Path, tmp_path: Path):
        """Single file with one @tool function."""
        _write_tool_file(
            tools_dir,
            "greet.py",
            """\
            from agno.tools import tool

            @tool(description="Say hello")
            def greet(name: str = "world") -> str:
                return f"Hello, {name}!"
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert len(toolkits) == 1
        assert "greet" in toolkits[0].functions

    def test_multiple_tools_in_one_file(self, tools_dir: Path, tmp_path: Path):
        """Multiple @tool functions in a single file become one toolkit."""
        _write_tool_file(
            tools_dir,
            "docker.py",
            """\
            from agno.tools import tool

            @tool(description="Start containers")
            def docker_up() -> str:
                return "up"

            @tool(description="Stop containers")
            def docker_down() -> str:
                return "down"
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert len(toolkits) == 1
        assert "docker_up" in toolkits[0].functions
        assert "docker_down" in toolkits[0].functions

    def test_multiple_files(self, tools_dir: Path, tmp_path: Path):
        """Each Python file becomes a separate toolkit."""
        _write_tool_file(
            tools_dir,
            "alpha.py",
            """\
            from agno.tools import tool

            @tool(description="Alpha")
            def alpha() -> str:
                return "a"
            """,
        )
        _write_tool_file(
            tools_dir,
            "beta.py",
            """\
            from agno.tools import tool

            @tool(description="Beta")
            def beta() -> str:
                return "b"
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert len(toolkits) == 2
        names = {tk.name for tk in toolkits}
        assert names == {"custom_alpha", "custom_beta"}

    def test_plain_functions_ignored(self, tools_dir: Path, tmp_path: Path):
        """Functions without @tool decorator are not picked up."""
        _write_tool_file(
            tools_dir,
            "mixed.py",
            """\
            from agno.tools import tool

            @tool(description="Real tool")
            def real_tool() -> str:
                return "real"

            def helper():
                return "not a tool"

            MY_CONSTANT = 42
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert len(toolkits) == 1
        assert "real_tool" in toolkits[0].functions
        assert "helper" not in toolkits[0].functions

    def test_underscore_files_skipped(self, tools_dir: Path, tmp_path: Path):
        """Files starting with _ are ignored."""
        _write_tool_file(
            tools_dir,
            "_internal.py",
            """\
            from agno.tools import tool

            @tool(description="Hidden")
            def hidden() -> str:
                return "hidden"
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert toolkits == []

    def test_broken_file_skipped(self, tools_dir: Path, tmp_path: Path):
        """A file with a syntax error is skipped gracefully."""
        _write_tool_file(
            tools_dir,
            "broken.py",
            """\
            def this is not valid python
            """,
        )
        _write_tool_file(
            tools_dir,
            "good.py",
            """\
            from agno.tools import tool

            @tool(description="Works")
            def good_tool() -> str:
                return "ok"
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert len(toolkits) == 1
        assert "good_tool" in toolkits[0].functions

    def test_file_with_no_tools_skipped(self, tools_dir: Path, tmp_path: Path):
        """A Python file with no @tool functions produces no toolkit."""
        _write_tool_file(
            tools_dir,
            "utils.py",
            """\
            def helper():
                return 42
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert toolkits == []

    def test_file_with_import_error_skipped(self, tools_dir: Path, tmp_path: Path):
        """A file that fails on import is skipped gracefully."""
        _write_tool_file(
            tools_dir,
            "bad_import.py",
            """\
            import nonexistent_module_xyz_12345
            from agno.tools import tool

            @tool(description="Never loaded")
            def unreachable() -> str:
                return "nope"
            """,
        )
        toolkits = load_custom_tools(tmp_path)
        assert toolkits == []


class TestCustomToolkit:
    def test_toolkit_name(self):
        @tool(description="Test")
        def my_fn() -> str:
            return "hi"

        tk = CustomToolkit(name="custom_test", functions=[my_fn])
        assert tk.name == "custom_test"
        assert "my_fn" in tk.functions

    def test_toolkit_functions_callable(self):
        @tool(description="Add two numbers")
        def add(a: int, b: int) -> int:
            return a + b

        tk = CustomToolkit(name="custom_math", functions=[add])
        func = tk.functions["add"]
        assert func is not None
