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
#: 59 when it was first counted. Two thirds of it is two clusters:
#: ``backend/server_artifacts.py`` and ``core/session/``. Neither is
#: unpicked one import at a time — they move with the capability or
#: not at all.
#:
#: Down to 53 once ``core/artifacts/`` stopped importing CodeIndex:
#: the Cypher guard both of them query through moved to
#: ``core/graph/``, and ``ARTIFACT_SCHEMA_STATEMENTS`` — Artifacts'
#: own DDL, defined inside CodeIndex's schema module — moved to
#: ``core/artifacts/graph_schema.py``. Two capabilities cannot be
#: extracted separately while one defines the other's schema.
MAY_NOT_GROW = 53


def test_the_measurement_finds_something():
    """A walk that finds nothing makes the ratchet meaningless."""
    assert len(_edges()) > 10


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


def test_most_of_the_work_is_in_two_files():
    """Recorded so the order is obvious from the test rather than
    from re-deriving it: this is not fifty-nine small jobs."""
    by_file: dict[str, int] = {}
    for rel, _, _, _ in _edges():
        by_file[rel] = by_file.get(rel, 0) + 1
    worst = sorted(by_file.items(), key=lambda kv: -kv[1])[:2]
    assert worst[0][0] == "backend/server_artifacts.py"
    assert worst[0][1] >= 15


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


@pytest.mark.xfail(
    reason=(
        "53 edges. `backend/controller_registry` imports `server_artifacts`, "
        "which is the capability's 57 RPCs — the host has no build without "
        "them yet. This is the target, and it is here rather than in a "
        "document so it is re-measured rather than re-asserted."
    ),
    strict=True,
)
def test_the_backend_starts_without_either_capability():
    done = _import_without(
        "ember_code.backend.app",
        "ember_code.core.artifacts",
        "ember_code.core.code_index",
    )
    assert done.returncode == 0, done.stderr[-800:]
