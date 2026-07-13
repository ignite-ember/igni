"""Custom tool loader — discovers @tool-decorated functions from .ember/tools/.

Security model
--------------
This module executes arbitrary Python code found in the user's
``~/.ember/tools/*.py`` and ``<project>/.ember/tools/*.py`` files
via ``importlib.util.spec_from_file_location`` + ``exec_module``.
There is intentionally NO sandboxing: the tool files come from
directories the user themselves put code in, on the machine they
run ember-code on. Treating them as untrusted would require full
process isolation (subprocess + seccomp / gVisor / etc.), which is
out of scope for a CLI developer tool.

What we *do* guard against:

* Only ``*.py`` files whose name does not start with ``_`` are
  loaded — matches the CC / plugin-loader convention.
* Files that raise on import are logged + skipped, not fatal.
* Plugin-contributed tool dirs get a namespaced toolkit name
  (``custom_<plugin>_<file>``) so a rogue plugin can't shadow the
  user's own tools.

What we do NOT guard against: a malicious tool file can do anything
the calling user can do. This matches Claude Code, JetBrains
Marketplace plugins, and every other extensible IDE — the trust
boundary is the file-system permissions on those directories, not
this loader.
"""

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from agno.tools import Toolkit
from agno.tools.function import Function

logger = logging.getLogger(__name__)


class CustomToolkit(Toolkit):
    """Wraps user-defined @tool functions into a single Agno Toolkit."""

    def __init__(self, name: str, functions: list, **kwargs: Any):
        super().__init__(name=name, **kwargs)
        for func in functions:
            self.register(func)


def _load_tools_from_file(file_path: Path) -> list:
    """Import a Python file and extract all @tool-decorated functions.

    Agno's @tool decorator converts functions into ``Function`` instances.
    We detect those to find user-defined tools.
    """
    module_name = f"ember_custom_tools.{file_path.stem}"

    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        logger.warning("Could not load module spec for %s", file_path)
        return []

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        logger.warning("Failed to load custom tool file %s: %s", file_path, e)
        # Clean up partial module
        sys.modules.pop(module_name, None)
        return []

    functions = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if isinstance(obj, Function):
            functions.append(obj)

    return functions


def load_custom_tools(
    project_dir: Path | None = None,
    *,
    plugin_tool_dirs: list[tuple[str, Path]] | None = None,
) -> list[Toolkit]:
    """Discover and load custom tools from .ember/tools/ directories.

    Scans directories in priority order (higher priority wins on conflicts):
    1. ~/.ember/tools/ (global user tools)
    2. <project>/.ember/tools/ (project tools)

    Plugins contribute additional tool directories via ``plugin_tool_dirs``
    as ``(plugin_name, tools_dir)`` tuples. Plugin toolkits are named
    ``custom_<plugin>_<file>`` so a plugin's tool file can never shadow
    or be shadowed by a same-named file in the user's own ``.ember/tools/``.

    Returns a list of Agno Toolkit instances, one per Python file.
    """
    if project_dir is None:
        project_dir = Path.cwd()

    dirs = [
        Path.home() / ".ember" / "tools",
        project_dir / ".ember" / "tools",
    ]

    toolkits: list[Toolkit] = []

    for tools_dir in dirs:
        if not tools_dir.is_dir():
            continue

        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            functions = _load_tools_from_file(py_file)
            if not functions:
                continue

            toolkit = CustomToolkit(
                name=f"custom_{py_file.stem}",
                functions=functions,
            )
            toolkits.append(toolkit)
            logger.info(
                "Loaded %d custom tool(s) from %s",
                len(functions),
                py_file,
            )

    for plugin_name, tools_dir in plugin_tool_dirs or []:
        if not tools_dir.is_dir():
            continue
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            functions = _load_tools_from_file(py_file)
            if not functions:
                continue

            toolkit = CustomToolkit(
                name=f"custom_{plugin_name}_{py_file.stem}",
                functions=functions,
            )
            toolkits.append(toolkit)
            logger.info(
                "Loaded %d custom tool(s) from plugin '%s' file %s",
                len(functions),
                plugin_name,
                py_file,
            )

    return toolkits
