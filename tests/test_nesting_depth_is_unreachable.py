"""``max_nesting_depth`` cannot fire, and the reason is structural.

``orchestration.max_nesting_depth`` defaults to 5 and is documented in the
generated config as "Max recursive sub-team levels". The guard that enforces it
is real::

    if self._current_depth >= self._max_depth:
        return f"Error: Maximum nesting depth ({self._max_depth}) reached."

but ``current_depth`` is passed through unchanged everywhere and never
incremented, so the obvious reading is a missing ``+ 1``. It is not. Adding the
increment would wire a counter that still cannot advance, because nothing below
depth 0 can spawn at all:

* ``OrchestrateTools`` — the toolkit carrying ``spawn_agent`` / ``spawn_team`` —
  is constructed in exactly one place, ``ToolsBuilder.orchestrate``, for the
  main agent, with ``current_depth=0`` hardcoded.
* A sub-team's members are ``copy.copy`` of pool agents. They are never given an
  ``OrchestrateTools``, so a spawned agent has no way to spawn anything.

So depth is always 0, the guard is always false, and the config is inert. The
sub-team is even named ``sub-team-depth-{current_depth + 1}``, which is what
makes this look like an oversight rather than a boundary.

This test states that boundary so it stays a decision. If someone gives spawned
agents their own orchestration toolkit, this fails and points at the counter
that has to start moving with it — the alternative being unbounded recursion
guarded by a setting everybody believes is already working.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ember_code"


def _construction_sites() -> list[tuple[str, int, str | None]]:
    """Every ``OrchestrateTools(...)`` call, with its ``current_depth`` argument."""
    sites: list[tuple[str, int, str | None]] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "OrchestrateTools"):
                continue
            depth = next(
                (ast.unparse(kw.value) for kw in node.keywords if kw.arg == "current_depth"),
                None,
            )
            sites.append((str(path.relative_to(SRC)), node.lineno, depth))
    return sites


def test_orchestration_is_built_once_at_depth_zero():
    sites = _construction_sites()

    assert len(sites) == 1, (
        "OrchestrateTools is now built in more than one place:\n"
        + "\n".join(f"  {f}:{line} current_depth={d}" for f, line, d in sites)
        + "\nIf a spawned agent can now spawn, nesting became reachable and "
        "orchestration.max_nesting_depth has to start counting — the guard in "
        "orchestrate_spawn.py reads current_depth, which has never moved off 0."
    )

    _, _, depth = sites[0]
    assert depth == "0", (
        f"the sole construction site now passes current_depth={depth!r}. If depth "
        "can vary, the guard is live and this test should be replaced by one that "
        "exercises it rather than documenting that it cannot fire."
    )
