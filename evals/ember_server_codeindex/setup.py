"""Eval setup hook — apply the ember-server JSONL snapshot to chroma.

Differs from ``evals.codeindex.setup`` in that we run the agent
against ember-server itself (a real git repo on disk, not a tmp
fixture copy). The setup hook:

1. Reads the prebuilt JSONL at /tmp/eval-comparison/ember-server.snapshot.jsonl
   (produced by ``ember-server/scripts/run_local_codeindex.py`` against
   ember-server's current HEAD).
2. Reads ember-server's actual git HEAD.
3. Rewrites the JSONL's commit op to match HEAD (in case the snapshot
   was generated from a different commit).
4. Applies the changeset to chroma at the ember-server-derived path,
   so when Session(project=ember-server).code_index queries, it
   finds populated data.

When ``EMBER_EVAL_NO_CODEINDEX=1`` is set, we bail before applying —
gives us the WITHOUT-codeindex baseline for the comparison report.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# The ember-server pipeline writes here. Override via env if relocated.
DEFAULT_SNAPSHOT_PATH = Path("/tmp/eval-comparison/ember-server.snapshot.jsonl")


async def setup(work_dir: Path, project_dir: Path) -> None:
    """Apply the JSONL snapshot to project_dir's chroma index.

    ``project_dir`` is ember-server (passed via the runner's
    ``--target-project-dir`` flag). The eval's per-case work_dir is
    not used here — the agent operates on project_dir directly.
    """
    snapshot_path = Path(os.environ.get("EMBER_EVAL_SERVER_SNAPSHOT") or DEFAULT_SNAPSHOT_PATH)
    if not snapshot_path.exists():
        raise RuntimeError(
            f"ember-server snapshot not found at {snapshot_path}. "
            "Build it first via ember-server/scripts/run_local_codeindex.py."
        )

    head_sha = _read_head(project_dir)
    logger.info(
        "ember-server eval setup: project=%s HEAD=%s snapshot=%s",
        project_dir,
        head_sha[:8],
        snapshot_path,
    )

    if os.environ.get("EMBER_EVAL_NO_CODEINDEX") == "1":
        logger.info(
            "EMBER_EVAL_NO_CODEINDEX=1 — skipping JSONL apply. "
            "Agent will fall back to shell on %s.",
            project_dir,
        )
        return

    # Rewrite the JSONL's commit_sha to match HEAD. The pipeline run
    # was supposed to use HEAD already, but be defensive — the agent's
    # tool gate checks ``has_commit(HEAD)``, not ``has_commit(<jsonl_sha>)``.
    rewritten = work_dir / ".ember-server-eval.jsonl"
    _rewrite_commit_sha(snapshot_path, rewritten, head_sha)

    from ember_code.core.code_index.index import CodeIndex

    index = CodeIndex(project=project_dir)
    try:
        # Idempotency: skip if chroma already has this commit. The
        # eval runner calls setup_module twice — once from
        # ``run_codeindex_eval.py`` pre-Session (so the tool gate
        # opens), once inside ``SuiteResult.run`` as the framework's
        # own hook. The second call would reinitialize chroma's
        # sentence-transformer embedder back-to-back in the same
        # process, which reliably hangs (no error, just stalled
        # threads). Skipping when chroma is already populated avoids
        # both the redundant work and the hang.
        if index.has_commit(head_sha):
            logger.info(
                "ember-server codeindex already has commit %s — skipping re-apply.",
                head_sha[:8],
            )
            return
        stats = await index.apply_delta(rewritten)
        logger.info(
            "ember-server codeindex populated: %d items, %d references",
            stats.items_upserted,
            stats.references_upserted,
        )
    finally:
        await index.close()


# ── Internals ─────────────────────────────────────────────────────────


def _read_head(project_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _rewrite_commit_sha(src: Path, dst: Path, new_sha: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
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
