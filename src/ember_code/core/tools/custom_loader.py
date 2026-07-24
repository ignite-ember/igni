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

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

# Runtime import (NOT ``TYPE_CHECKING``): ``Function`` is used in
# the ``isinstance`` sniff below, not just in annotations.
from agno.tools.function import Function

from ember_code.core.tools.custom_loader_schemas import (
    DiscoveryResult,
    FailedFile,
    LoadedFile,
    SkippedFile,
    ToolSource,
)

# Re-export the schemas so external callers with
# ``from ember_code.core.tools.custom_loader import DiscoveryResult``
# work without needing to know the sibling schemas module exists.
__all__ = [
    "CustomToolLoader",
    "CustomToolkit",
    "DiscoveryResult",
    "FailedFile",
    "LoadedFile",
    "SkippedFile",
    "ToolSource",
    "load_custom_tools",
]

logger = logging.getLogger(__name__)


class CustomToolkit(Toolkit):
    """Wraps user-defined @tool functions into a single Agno Toolkit.

    Keeps ``**kwargs`` forwarding to :class:`agno.tools.Toolkit`
    (accepted Rule 4 deviation) — a typed ``CustomToolkitConfig``
    here would duplicate agno's kwargs list and violate the
    framework boundary. Positional order ``(name, functions)`` is
    preserved for back-compat with tests and callers that
    construct the toolkit directly.
    """

    def __init__(self, name: str, functions: list[Function], **kwargs: Any):
        super().__init__(name=name, **kwargs)
        for func in functions:
            self.register(func)


class CustomToolLoader:
    """Coordinator for a custom-tool discovery run.

    Owns the ``sys.modules`` bookkeeping (:attr:`_loaded_module_names`
    + :meth:`unload`) so a caller — typically a plugin-reload path —
    can drop the imported modules explicitly. Iterates a uniform
    list of :class:`ToolSource` entries (user dirs + plugin dirs)
    and returns a :class:`DiscoveryResult` with the built toolkits
    alongside diagnostic lists of loaded / skipped / failed files.

    See the module docstring for the security model — this class
    executes arbitrary Python from the user's
    ``.ember/tools/`` dirs without sandboxing. The trust boundary
    is the file-system permissions on those directories, not this
    loader.

    Instances are single-use in the sense that :attr:`_loaded_module_names`
    accumulates across successive :meth:`discover` calls — this is
    intentional (so :meth:`unload` can drop *everything* this
    loader put on ``sys.modules``), not a bug. Callers wanting a
    fresh view construct a new :class:`CustomToolLoader`.
    """

    def __init__(self) -> None:
        # Track every module name we installed on ``sys.modules`` so
        # a future plugin-reload flow can call :meth:`unload` to
        # drop them. NOT auto-invoked at the end of :meth:`discover`
        # — loaded :class:`Function` objects hold references to
        # module-level state (closures, imports) and dropping the
        # module would break ``@tool`` handlers at call time.
        # ``unload()`` is opt-in only.
        self._loaded_module_names: list[str] = []

    # ── public API ───────────────────────────────────────────────

    def discover(
        self,
        project_dir: Path | None = None,
        *,
        plugin_tool_dirs: list[tuple[str, Path]] | None = None,
    ) -> DiscoveryResult:
        """Discover custom tools from ``.ember/tools/`` directories.

        Scans directories in priority order (higher priority wins
        on conflicts — priority is enforced by the *caller* over
        the returned toolkits; this loader emits them in the same
        priority order):

        1. ``~/.ember/tools/`` (global user tools)
        2. ``<project>/.ember/tools/`` (project tools)
        3. Plugin-contributed tool dirs (namespaced
           ``custom_<plugin>_<file>``)

        Within each directory, files are scanned in ``sorted()``
        order so the returned toolkit list is deterministic
        run-to-run.
        """
        if project_dir is None:
            project_dir = Path.cwd()

        # Preserve the exact iteration order documented in the
        # module docstring: home dir -> project dir -> plugin dirs.
        # Any agent tool-resolution priority downstream depends on
        # this order — an accidental reshuffle would silently
        # rewire which tool wins on name conflicts.
        sources: list[ToolSource] = [
            ToolSource(name_prefix="custom", tools_dir=Path.home() / ".ember" / "tools"),
            ToolSource(name_prefix="custom", tools_dir=project_dir / ".ember" / "tools"),
        ]
        for plugin_name, tools_dir in plugin_tool_dirs or []:
            sources.append(
                ToolSource(
                    name_prefix=f"custom_{plugin_name}",
                    tools_dir=tools_dir,
                )
            )

        result = DiscoveryResult()
        for source in sources:
            self._scan_source(source, result)
        return result

    def unload(self) -> None:
        """Drop every module this loader installed on ``sys.modules``.

        Opt-in only — see the ``_loaded_module_names`` comment in
        :meth:`__init__`. Intended for a future plugin-reload flow
        that wants a clean slate before re-running :meth:`discover`.
        Never call this while any of the loaded ``@tool`` handlers
        might still be invoked: their closures hold references
        into the module, and dropping it breaks them.
        """
        for module_name in self._loaded_module_names:
            sys.modules.pop(module_name, None)
        self._loaded_module_names.clear()

    # ── internals ────────────────────────────────────────────────

    def _scan_source(self, source: ToolSource, result: DiscoveryResult) -> None:
        """Scan a single :class:`ToolSource` into ``result``.

        Records a ``not_a_directory`` :class:`SkippedFile` for a
        missing / non-dir path so the caller can distinguish
        "you asked us to scan a dir that doesn't exist" from
        "the dir exists but has no tool files".
        """
        if not source.tools_dir.is_dir():
            result.skipped.append(SkippedFile(path=source.tools_dir, reason="not_a_directory"))
            return

        for py_file in sorted(source.tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                result.skipped.append(SkippedFile(path=py_file, reason="underscore_prefix"))
                continue

            functions = self._load_file(py_file, result)
            if functions is None:
                # Load failed — :meth:`_load_file` already recorded
                # the :class:`FailedFile` on ``result``.
                continue

            if not functions:
                result.skipped.append(SkippedFile(path=py_file, reason="no_functions"))
                continue

            toolkit_name = f"{source.name_prefix}_{py_file.stem}"
            toolkit = CustomToolkit(name=toolkit_name, functions=functions)
            result.toolkits.append(toolkit)
            result.loaded.append(
                LoadedFile(
                    path=py_file,
                    toolkit_name=toolkit_name,
                    function_count=len(functions),
                )
            )
            # Preserve the pre-refactor log lines verbatim — user
            # debugging habits (``grep 'custom tool' ~/.ember/logs``)
            # depend on the exact message shape. The plugin variant
            # is emitted when the source's name_prefix carries a
            # plugin namespace.
            if source.name_prefix == "custom":
                logger.info(
                    "Loaded %d custom tool(s) from %s",
                    len(functions),
                    py_file,
                )
            else:
                # ``source.name_prefix`` == ``f"custom_{plugin_name}"``
                plugin_name = source.name_prefix.removeprefix("custom_")
                logger.info(
                    "Loaded %d custom tool(s) from plugin '%s' file %s",
                    len(functions),
                    plugin_name,
                    py_file,
                )

    def _load_file(
        self,
        file_path: Path,
        result: DiscoveryResult,
    ) -> list[Function] | None:
        """Import a Python file and extract all @tool-decorated functions.

        Agno's ``@tool`` decorator converts functions into
        :class:`Function` instances. We detect those to find
        user-defined tools.

        On failure records a :class:`FailedFile` on ``result`` so
        the caller (registry / UI) can surface the error to the
        user instead of requiring them to tail the log, and
        returns ``None``. On success returns the list of
        discovered :class:`Function` instances (possibly empty).
        """
        module_name = f"ember_custom_tools.{file_path.stem}"

        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            logger.warning("Could not load module spec for %s", file_path)
            result.failed.append(
                FailedFile(
                    path=file_path,
                    error="importlib returned no spec/loader for this path",
                    error_type="ImportError",
                )
            )
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self._loaded_module_names.append(module_name)

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            logger.warning("Failed to load custom tool file %s: %s", file_path, e)
            # Clean up partial module and forget we ever installed
            # it so :meth:`unload` doesn't try to double-remove.
            sys.modules.pop(module_name, None)
            if self._loaded_module_names and self._loaded_module_names[-1] == module_name:
                self._loaded_module_names.pop()
            result.failed.append(
                FailedFile(
                    path=file_path,
                    error=str(e),
                    error_type=type(e).__name__,
                )
            )
            return None

        functions: list[Function] = []
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
    """Back-compat shim — returns a plain ``list[Toolkit]``.

    Existing callers (:mod:`registry`, :mod:`tools_builder`) rely
    on this exact return shape:

    * :meth:`ToolRegistry.load_custom_tools` returns the value
      unchanged as ``list[Toolkit]``.
    * :meth:`ToolkitAssembler.custom` wraps the value in
      ``list(loaded)`` — passing a :class:`DiscoveryResult`
      Pydantic model here would silently iterate the model's
      *fields* (``["toolkits", "loaded", "skipped", "failed"]``)
      instead of the toolkits, breaking tools_builder at runtime.

    New callers should prefer
    :meth:`CustomToolLoader.discover` for the structured result
    shape (loaded / skipped / failed).
    """
    loader = CustomToolLoader()
    result = loader.discover(project_dir, plugin_tool_dirs=plugin_tool_dirs)
    # Explicit ``list(...)`` guards the caller contract: if a
    # future refactor changes :attr:`DiscoveryResult.toolkits` to
    # a non-list sequence, the shim still hands back a plain
    # ``list[Toolkit]`` so ``tools_builder.py:custom()``'s
    # ``list(loaded)`` doesn't degrade.
    return list(result.toolkits)
