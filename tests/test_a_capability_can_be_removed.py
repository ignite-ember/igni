"""How far the host still is from working without a capability.

`EXTRACTING_A_CAPABILITY.md` said two module-scope imports blocked
extraction:

    core/agents/builder.py      from ...tools.artifacts import ArtifactTools
    core/tools/tool_spec.py     from ...tools.codeindex import CodeIndexTools

Both are real. Neither is the problem. Measured with an AST walk
rather than a grep, the host reaches into CodeIndex and Artifacts in
**59 places across 17 files**, 37 of them at module scope — and the
largest cluster is `backend/server_artifacts.py`, which is the
57-RPC surface that has to move wholesale rather than be unpicked.

So this file does not assert separability. It measures it, because a
number that can only go down is the thing that was missing: the
"two imports" figure went unchallenged long enough to become the
plan, and cutting those two does not make the backend start.

**Why there are no `try: import … except ImportError` sentinels.**
There were, briefly, at the two imports above. They made those two
call sites tolerate absence while fifty-seven other edges did not,
which bought a passing test and no separability — and left the next
reader a pattern suggesting the work was nearly done. A capability's
symbols belong in that capability's extension. Until they are there,
the host importing them directly is the honest spelling.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src/ember_code"

#: The two capabilities being extracted, as import prefixes. Each has
#: a ``core/`` package and a ``core/tools/`` toolkit.
CAPABILITIES = (
    "ember_code.core.tools.codeindex",
    "ember_code.core.tools.artifacts",
    "ember_code.core.code_index",
    "ember_code.core.artifacts",
)

#: Directories that *are* the capabilities, so they are not counted
#: for importing themselves.
OWN = (
    "core/tools/codeindex/",
    "core/tools/artifacts",
    "core/code_index/",
    "core/artifacts/",
)


def _edges() -> list[tuple[str, int, str, bool]]:
    """``(file, line, module, at_module_scope)`` for every edge.

    An AST walk rather than a grep, which is not pedantry: the edge
    this found first was an import *inside a function the class body
    calls*, so the module imported the capability while a grep for a
    leading ``from`` saw nothing.
    """
    found: list[tuple[str, int, str, bool]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = str(path.relative_to(_SRC))
        if rel.startswith(OWN):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover — defensive
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            for module in modules:
                if module.startswith(CAPABILITIES):
                    found.append((rel, node.lineno, module, node.col_offset == 0))
    return found


#: **This number may go down. It may not go up.**
#:
#: 59 when it was first counted, 53 once the shared Cypher layer
#: moved to ``core/graph/``, and 18 once Artifacts left: its 12,700
#: lines and its 3,500-line RPC controller are in
#: ``igni__artifacts/extension`` and the host keeps only the
#: fifty-nine legacy RPC *names*, routed there by
#: ``backend/rpc_handlers/artifacts.py``.
#:
#: 8 once CodeIndex's panel, its ``codeindex_*`` RPCs and ``/codeindex``
#: left for its extension's page (``igni__code-index/extension``).
#:
#: What is left is CodeIndex's local index and the agent tool that
#: reads it, which move when the index does.
MAY_NOT_GROW = 8


def test_the_measurement_finds_something():
    """A walk that finds nothing makes the ratchet meaningless."""
    assert len(_edges()) > 0


def test_the_host_is_not_growing_new_edges_into_a_capability():
    edges = _edges()
    if len(edges) <= MAY_NOT_GROW:
        return
    listed = "\n  ".join(f"{f}:{line} {mod}" for f, line, mod, _ in edges)
    raise AssertionError(
        f"{len(edges)} edges into CodeIndex/Artifacts, up from {MAY_NOT_GROW}:\n  "
        f"{listed}\n"
        "Both are being extracted. A new edge is a new thing to cut later."
    )


def test_the_ratchet_is_tight():
    """A ceiling above the real count stops catching anything."""
    assert len(_edges()) == MAY_NOT_GROW, (
        "the count moved. If edges were cut, lower MAY_NOT_GROW to match — that is the point of it."
    )


def test_nothing_reaches_into_artifacts_any_more():
    """The capability that left. Not a count — zero.

    ``backend/server_artifacts.py`` held twenty of the original
    fifty-nine and was the reason "cut the two imports" was the
    wrong first step. It is gone, with ``core/artifacts/`` and
    ``core/tools/artifacts.py``.
    """
    reaching = sorted(f"{rel}:{line}" for rel, line, mod, _ in _edges() if "artifacts" in mod)
    assert not reaching, reaching


# ── The goal, kept in the suite rather than in a document ─────────
#
# In a subprocess, and that is not a detail. The first version of
# this blocked the import inside the test process by evicting
# ``ember_code.core*`` from ``sys.modules`` and restoring it after.
# It worked, and it quietly broke two unrelated tests later in the
# same worker, which then passed on their own and failed in the
# suite — the worst shape a test failure has. A claim about what a
# *process* can import belongs in a process of its own.

BLOCK_AND_IMPORT = """
import importlib, sys

class Blocked:
    def __init__(self, package):
        self.package = package

    def find_spec(self, name, path=None, target=None):
        if name == self.package or name.startswith(self.package + "."):
            raise ImportError(name + " is not installed (blocked)")
        return None

for package in {packages!r}:
    sys.meta_path.insert(0, Blocked(package))

importlib.import_module({module!r})
print("ok")
"""


def _import_without(module: str, *packages: str) -> subprocess.CompletedProcess[str]:
    """Import ``module`` in a fresh interpreter with ``packages`` gone."""
    script = BLOCK_AND_IMPORT.format(packages=list(packages), module=module)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        timeout=120,
    )


def test_the_blocker_actually_blocks():
    """Otherwise the xfail below would be passing for free."""
    done = _import_without("ember_code.core.artifacts", "ember_code.core.artifacts")
    assert done.returncode != 0
    assert "is not installed (blocked)" in done.stderr


def test_a_capability_imports_normally_when_it_is_there():
    done = _import_without("ember_code.core.artifacts", "nothing.at.all")
    assert done.returncode == 0, done.stderr[-800:]


def test_the_backend_starts_without_artifacts():
    """The one this file was written to fail.

    It did, for as long as `backend/controller_registry` imported
    `server_artifacts`. Both are gone: the fifty-nine RPCs are
    served by the extension and igni keeps only their names.
    """
    done = _import_without("ember_code.backend.app", "ember_code.core.artifacts")
    assert done.returncode == 0, done.stderr[-800:]


@pytest.mark.xfail(
    reason=(
        "CodeIndex has not left yet. `core/code_index/` is still the local "
        "index and `core/tools/codeindex/` its agent tool; its page, RPCs "
        "and command have moved to the extension. The index moves with the "
        "graph-db sidecar."
    ),
    strict=True,
)
def test_the_backend_starts_without_codeindex():
    done = _import_without("ember_code.backend.app", "ember_code.core.code_index")
    assert done.returncode == 0, done.stderr[-800:]
