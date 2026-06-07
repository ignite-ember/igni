"""Eval setup hook — populates the local CodeIndex from a snapshot JSONL.

The eval framework calls :func:`setup` after copying the fixture repo
into the per-case ``work_dir`` but before any case runs. We:

1. Initialize a git repo in ``work_dir`` and commit the fixture files
   so HEAD has a real SHA — the Session's CodeIndex tooling looks at
   ``git HEAD`` to decide which ``<sha>.chroma/`` to query.
2. Read the committed JSONL snapshot at
   ``evals/fixtures/codeindex_repo.snapshot.jsonl`` (real LLM output
   from a one-time run of the server pipeline against this fixture).
3. Rewrite the snapshot's ``commit`` op so its ``sha`` matches the
   freshly-minted HEAD. The quality / category data doesn't depend
   on the SHA, so this is safe.
4. ``apply_delta`` the rewritten JSONL — populates chroma + the
   reference SQLite under ``~/.ember/projects/<derived>/``. The
   agent's Session, when it starts in ``work_dir``, will derive the
   same project_id and find the populated index.

If you change the fixture's source code, regenerate the snapshot via
``ember-server/scripts/run_local_codeindex.py`` and commit the new
``codeindex_repo.snapshot.jsonl``. The plumbing test
(``tests/test_codeindex_eval_fixture.py``) still uses the deterministic
pydantic-builder fixture — it doesn't touch the snapshot.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPSHOT_RELATIVE_PATH = "evals/fixtures/codeindex_repo.snapshot.jsonl"


async def setup(work_dir: Path, project_dir: Path) -> None:
    """Initialize git + apply the JSONL snapshot to populate the local index.

    When ``EMBER_EVAL_NO_CODEINDEX=1`` is set, we still git-init the
    work_dir (Session needs a real HEAD to construct
    ``CodeIndexSyncManager``) but skip the apply_delta step. Without a
    populated chroma dir, the tool gate in
    ``session.core._codeindex_available`` returns ``False`` and
    ``codeindex_query`` doesn't appear on the agent's tool list. That's
    the comparison condition for the CEO report.
    """
    snapshot_path = project_dir / SNAPSHOT_RELATIVE_PATH
    if not snapshot_path.exists():
        raise RuntimeError(
            f"codeindex eval snapshot not found at {snapshot_path}. "
            "Regenerate via ember-server/scripts/run_local_codeindex.py."
        )

    # 1. git init + commit so HEAD has a real SHA.
    head_sha = _git_init_and_commit(work_dir)

    # Bail before chroma if the comparison run wants no codeindex.
    import os

    if os.environ.get('EMBER_EVAL_NO_CODEINDEX') == '1':
        logger.info(
            "codeindex eval setup: SKIPPING JSONL apply (EMBER_EVAL_NO_CODEINDEX=1) — "
            "agent will fall back to shell/grep. HEAD=%s",
            head_sha[:8],
        )
        return

    # 2. Rewrite the snapshot's commit op to match the new HEAD.
    rewritten_jsonl = work_dir / ".eval-codeindex.jsonl"
    _rewrite_commit_sha(snapshot_path, rewritten_jsonl, head_sha)

    # 3. Apply the changeset to chroma + SQLite. Uses the same
    #    ``~/.ember`` data_dir the agent's Session will read from.
    from ember_code.core.code_index.index import CodeIndex

    index = CodeIndex(project=work_dir)
    try:
        stats = await index.apply_delta(rewritten_jsonl)
        logger.info(
            "codeindex eval populated: %d items, %d references → HEAD=%s",
            stats.items_upserted,
            stats.references_upserted,
            head_sha[:8],
        )
    finally:
        await index.close()


# ── Internals ───────────────────────────────────────────────────────


def _git_init_and_commit(work_dir: Path) -> str:
    """Initialize a git repo in ``work_dir`` and return the new HEAD sha.

    Idempotent: if ``work_dir`` is already a git repo, this re-uses
    the existing HEAD. Otherwise it inits, commits everything, and
    returns the resulting sha.
    """
    git_dir = work_dir / ".git"
    if not git_dir.exists():
        env = {"GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.com",
               "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.com"}
        _run_git(work_dir, ["init", "--initial-branch=main"], env=env)
        _run_git(work_dir, ["add", "."], env=env)
        _run_git(work_dir, ["commit", "-m", "eval fixture initial commit"], env=env)

    head = _run_git(work_dir, ["rev-parse", "HEAD"]).stdout.strip()
    return head


def _run_git(cwd: Path, args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = None
    if env:
        import os

        full_env = {**os.environ, **env}
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    )


def _rewrite_commit_sha(src: Path, dst: Path, new_sha: str) -> None:
    """Copy ``src`` JSONL to ``dst`` with the commit op's sha replaced.

    Only the first line (``commit`` op) needs the rewrite — every
    subsequent op uses opaque UUIDs that don't depend on the sha.
    """
    with src.open() as fh_in, dst.open("w") as fh_out:
        first = next(fh_in)
        op = json.loads(first)
        if op.get("op") != "commit":
            raise RuntimeError(
                f"expected first JSONL op to be 'commit', got {op.get('op')!r}"
            )
        op["sha"] = new_sha
        fh_out.write(json.dumps(op) + "\n")
        shutil.copyfileobj(fh_in, fh_out)
