"""The data layout is the host's, not CodeIndex's.

``state.db`` holds the scheduler, the session list, ``/loop`` and the
background-process registry. None of that is an index, and all of it
used to be found by importing ``core.code_index.paths`` — because
that is where a data path *was*, and nobody relocates a directory to
add one lookup.

The count grew from four modules to nine while nothing in particular
happened. That is the shape of the problem: CodeIndex is becoming an
extension, and an extension is a thing the host can be built
*without*. A host that cannot find its own scheduler database without
importing CodeIndex is not one you can uninstall CodeIndex from.

Two rules, and the second is the one that will actually catch
something. The first pins the split. The second is a ratchet: the
number of modules outside ``core/code_index/`` that import it may go
down and may not go up, so the extraction cannot quietly lose ground
between the sessions that work on it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ember_code.core import code_index, data_paths

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src/ember_code"
_INDEX_PACKAGE = _SRC / "core/code_index"


def _imports_of(path: Path) -> set[str]:
    """Every ``ember_code`` module this file imports, however spelled."""
    found: set[str] = set()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _importers_outside_the_package() -> list[str]:
    out: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if _INDEX_PACKAGE in path.parents:
            continue
        if any(m.startswith("ember_code.core.code_index") for m in _imports_of(path)):
            out.append(str(path.relative_to(_SRC)))
    return out


class TestTheSplitHolds:
    def test_the_host_layout_is_not_in_the_index_package(self):
        """A path a host needs must not live behind an extension."""
        index_paths = __import__("ember_code.core.code_index.paths", fromlist=["paths"])
        for name in (
            "state_db_path",
            "data_root",
            "global_db_path",
            "resolve_project_id",
            "neo4j_data_dir",
            "neo4j_state_root",
        ):
            assert not hasattr(index_paths, name), (
                f"'{name}' is back in core/code_index/paths.py. It is the host's "
                "— it belongs in core/data_paths.py, which is below the index "
                "rather than inside it."
            )

    def test_the_index_keeps_what_is_actually_its_own(self):
        from ember_code.core.code_index import paths as index_paths

        assert sorted(index_paths.__all__) == [
            "code_index_dir",
            "legacy_commit_index_path",
            "manifest_path",
            "neo4j_migrated_marker_path",
        ]

    def test_the_layout_module_does_not_reach_back(self):
        """``data_paths`` sits below the index, so it cannot import it
        — otherwise the cycle is the same coupling with one more hop."""
        reached = _imports_of(Path(data_paths.__file__))
        assert not [m for m in reached if m.startswith("ember_code.core.code_index")]

    def test_the_package_is_still_there_to_measure(self):
        """A rename that emptied the package would make the ratchet
        below pass by accident."""
        assert Path(code_index.__file__).parent == _INDEX_PACKAGE


#: Modules outside ``core/code_index/`` that still import it.
#:
#: **This number may go down. It may not go up.** It was 30 before
#: the data layout moved out; nine modules — the scheduler, memory,
#: ``/loop``, the process store, client state, session directories,
#: the Neo4j runtime, the bootstrap and the artifact index — stopped
#: importing CodeIndex entirely, because all they ever wanted was
#: ``state.db``.
#:
#: Down again to 19 once the shared Cypher layer left: the guard
#: both capabilities query through is ``core/graph/`` now, not
#: something Artifacts imported out of CodeIndex.
#:
#: Most of what is left is CodeIndex talking to itself across two
#: directories: ``core/tools/codeindex/`` is its agent tool and
#: ``backend/server_codeindex.py`` its RPCs, and both leave with it.
#: The rest is the real remaining work.
MAY_NOT_GROW = 19


def test_the_host_is_not_growing_new_dependencies_on_the_index():
    importers = _importers_outside_the_package()
    assert len(importers) <= MAY_NOT_GROW, (
        f"{len(importers)} modules outside core/code_index/ import it, up from "
        f"{MAY_NOT_GROW}:\n  " + "\n  ".join(importers) + "\n"
        "CodeIndex is being extracted. A new edge into it is a new thing to "
        "cut later — put what you need in a host module instead."
    )


def test_the_ratchet_is_tight():
    """A ceiling well above the real count stops catching anything."""
    assert len(_importers_outside_the_package()) == MAY_NOT_GROW, (
        "the count moved. If an edge was cut, lower MAY_NOT_GROW to match — "
        "that is the point of it."
    )
