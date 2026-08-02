"""Eval setup for the data-architect Cypher authoring suite.

The ``codeindex.cypher`` eval (codeindex.yaml) exercises the typed
``codeindex_query`` surface — the cypher eval (this file) exercises
the raw ``codeindex_cypher`` surface that the data-architect agent
is the curated owner of. We share the ``codeindex_repo`` fixture
so the agent has a real schema to author against.

The ``codeindex_cypher`` tool routes through
``CodeIndex.client_for(commit)`` which dispatches to a
``Neo4jClient.execute_query(cypher, **params)``. We don't bring up
a real Neo4j for the eval — that's a heavy dependency for CI. The
``Neo4jClient`` is mocked per-case with a stub that:
  1. records every call (so the runner can inspect params + cypher);
  2. returns canned rows keyed off the question's intent, so the
     assertion's "result shape matches" check has something
     concrete to grade.

The agent goes through the full agno pipeline (real model, real
tool-call machinery, real telemetry). The only thing stubbed
is the *driver* itself — which is exactly the boundary we want
to verify across (because the agent can't tell the difference
between a real driver and a stub, but we can).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

logger = logging.getLogger(__name__)


# Per-case canned responses. The runner installs one of these on
# the mocked ``Neo4jClient.execute_query`` based on the case's
# ``input`` prefix. Each is a small list of dict rows that match
# the question's expected shape.
_CANONICAL_RESPONSES: dict[str, list[dict]] = {
    "audit-by-shape": [
        {"path": "src/auth/login.py", "item_id": "item-a1", "vulnerabilities": ["sql-injection"]},
        {"path": "src/web/upload.py", "item_id": "item-a2", "vulnerabilities": ["path-traversal"]},
        {"path": "src/legacy/parser.py", "item_id": "item-a3", "quality": "very-high"},
    ],
    "multi-hop": [
        {"path": "src/auth/login.py:authenticate", "item_id": "item-m1"},
        {"path": "src/db/queries.py:list_users_with_orders", "item_id": "item-m2"},
        {"path": "src/api/router.py:handle_request", "item_id": "item-m3"},
    ],
    "aggregates": [
        {"domain": "auth", "count": 4},
        {"domain": "db", "count": 6},
        {"domain": "api", "count": 5},
    ],
    "path-structural": [
        {"path": "src/auth/", "type": "folder"},
        {"path": "src/db/", "type": "folder"},
    ],
}


async def setup(work_dir: Path, project_dir: Path) -> None:
    """Stub the Neo4j client on the agent's CodeIndex instance.

    Called after the fixture copy but before any case runs. The
    agent's session will derive the same project_id from the
    work_dir git HEAD; the ``client_for`` is intercepted so the
    agent's calls are answered by ``_CANONICAL_RESPONSES``
    instead of touching a real database.
    """
    from ember_code.core.code_index.index import CodeIndex  # noqa: PLC0415 — lazy

    # 1. git init + commit so HEAD has a real SHA (the data-architect
    #    reads the same fixture the codeindex eval does).
    head_sha = _git_init_and_commit(work_dir)
    logger.info("data-architect cypher eval: HEAD=%s", head_sha[:8])

    # 2. Patch the *singleton* CodeIndex (the agent's session creates
    #    its own; we patch client_for on the class so every instance
    #    gets the stub). The class is per-process but the data-architect
    #    agent always runs in this process, so this is safe.
    client = MagicMock()
    client.execute_query = AsyncMock(side_effect=_dispatch)
    # The class-level attribute is what ``_client_for`` reads first
    # before instantiating a per-commit client. Setting it short-
    # circuits the per-commit driver spawn.
    # Type: ignore because ``_neo4j_client`` is typed as Optional
    # but mypy is sensitive to the override.
    CodeIndex._neo4j_client = client  # type: ignore[assignment]


async def _dispatch(cypher: str, **params: object) -> list[dict]:
    """Canned-response dispatcher for the mocked driver.

    Picks a response shape based on the question category the
    eval case is in. The agent's emitted cypher is logged
    for the runner to inspect via the tool trace — this
    function is what the agent sees the database return.
    """
    cypher_lc = cypher.lower()
    if "security" in cypher_lc or "vulnerabilit" in cypher_lc or "refactor" in cypher_lc:
        return _CANONICAL_RESPONSES["audit-by-shape"]
    if "callers" in cypher_lc or "call" in cypher_lc or "blast" in cypher_lc or "travers" in cypher_lc:
        return _CANONICAL_RESPONSES["multi-hop"]
    if "count" in cypher_lc or "sum" in cypher_lc or "collect" in cypher_lc or "domain" in cypher_lc:
        return _CANONICAL_RESPONSES["aggregates"]
    if "folder" in cypher_lc or "path" in cypher_lc or "class_definition" in cypher_lc:
        return _CANONICAL_RESPONSES["path-structural"]
    # Default: empty result. The agent should pick a query shape
    # that maps to one of the four cases — landing here means
    # the model authored Cypher the eval case doesn't test.
    return []


def _git_init_and_commit(work_dir: Path) -> str:
    """Initialize a git repo in ``work_dir`` and return the new HEAD sha.

    Mirrors the helper in ``evals.codeindex.setup`` so the
    data-architect's read of ``git HEAD`` resolves. Idempotent:
    re-uses an existing HEAD if ``work_dir`` is already a repo.
    """
    import os
    import subprocess

    git_dir = work_dir / ".git"
    if not git_dir.exists():
        env = {
            "GIT_AUTHOR_NAME": "eval",
            "GIT_AUTHOR_EMAIL": "eval@example.com",
            "GIT_COMMITTER_NAME": "eval",
            "GIT_COMMITTER_EMAIL": "eval@example.com",
        }
        full_env = {**os.environ, **env}
        for args in (
            ["init", "--initial-branch=main"],
            ["add", "."],
            ["commit", "-m", "eval fixture initial commit"],
        ):
            r = subprocess.run(
                ["git", *args], cwd=str(work_dir), env=full_env,
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                logger.warning(
                    "data-architect cypher eval: git %s returned rc=%d "
                    "stderr=%s (proceeding; CodeIndex is mocked anyway)",
                    args,
                    r.returncode,
                    r.stderr[:200],
                )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(work_dir),
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        # No commit yet — the commit step earlier may have been a
        # no-op (sandbox env stripped our git config, etc.). The
        # CodeIndex is mocked anyway, so the HEAD sha is decorative.
        logger.warning(
            "data-architect cypher eval: no HEAD available "
            "(returning empty sha; CodeIndex is mocked)"
        )
        return ""
    return out.stdout.strip()
