"""Load ember-server changesets into per-commit Neo4j, without the cloud.

Why this exists: the fine-tuning corpus needs real CodeIndex data. Production
gets it by having ember-server upload a per-commit JSONL changeset to GCS and
ember-code's ``CodeIndexSyncManager.sync_now()`` pull it through a signed URL
and replay it. For local dataset work that round trip buys nothing — the
changesets already sit on disk — and it costs an interactive cloud login.

So this does the half that matters: the same ``CodeIndex.apply_delta`` the sync
manager calls once the download finishes, against the same per-(project, commit)
Neo4j process the trace executor later queries.

The keying is the part worth being careful about. ``igni__fine-tuning``'s
``CodeIndexRealExecutor`` finds a graph by ``(project_id, sha)``, where
``project_id`` hashes the substrate's ``git remote get-url origin`` and ``sha``
is the substrate clone's own ``HEAD``. A substrate parked on a different commit
than its changeset describes would send every query to a graph about code the
trace never read — with no error anywhere. This script refuses to load in that
case rather than producing quietly wrong training data.

Usage:
    uv run python scripts/load_changesets.py --verify-only
    uv run python scripts/load_changesets.py --only sqlparse
    uv run python scripts/load_changesets.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DEFAULT_CHANGESETS = Path("~/ai_coding/codeindex-changesets").expanduser()
DEFAULT_SUBSTRATES = Path("~/ai_coding/igni__fine-tuning/finetune/substrates").expanduser()
DEFAULT_DATA_DIR = Path("~/.ember").expanduser()


@dataclass(frozen=True)
class Target:
    repo: str
    substrate: Path
    changeset: Path
    sha: str


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def _targets(changesets: Path, substrates: Path, only: str | None) -> list[Target]:
    manifest = json.loads((changesets / "manifest.json").read_text())
    targets: list[Target] = []
    for repo, meta in sorted(manifest.items()):
        if only and repo != only:
            continue
        targets.append(
            Target(
                repo=repo,
                substrate=substrates / repo,
                changeset=changesets / meta["file"],
                sha=meta["sha"],
            )
        )
    return targets


def _check(target: Target) -> str | None:
    """Return a reason the target cannot be loaded, or None when it is sound."""
    if not target.changeset.is_file():
        return f"changeset missing: {target.changeset}"
    if not (target.substrate / ".git").is_dir():
        return f"substrate not cloned: {target.substrate}"
    head = _git(["rev-parse", "HEAD"], target.substrate)
    if head != target.sha:
        return f"substrate is at {head[:12]}, changeset describes {target.sha[:12]}"
    if not _git(["remote", "get-url", "origin"], target.substrate):
        return "substrate has no origin remote, so project_id would hash its path instead"
    return None


async def _load(target: Target, data_dir: Path, verify_only: bool, grace: float = 120.0) -> str:
    from ember_code.backend.neo4j_runtime import Neo4jRuntime
    from ember_code.core.code_index.embedder import LiveEmbedder
    from ember_code.core.code_index.index import CodeIndex
    from ember_code.core.code_index.project import resolve_project_id

    project_id = resolve_project_id(target.substrate)
    # The default shutdown grace is 10s, and the runtime SIGKILLs when it
    # expires. Neo4j checkpoints during a *clean* shutdown, so a kill discards
    # the load: five repositories were loaded here, queried successfully while
    # the process was up, and came back empty on the next start for exactly that
    # reason. ``db.checkpoint`` would be the explicit fix but it is Enterprise
    # only, so the grace has to be long enough to let it finish on its own.
    runtime = Neo4jRuntime(data_dir=data_dir, shutdown_grace_sec=grace)
    # Real vectors, not hashes: every graph loaded before this carried
    # HashEmbedder chunks, which is why 2,255 evaluation queries never once used
    # the vector index — it could not have worked.
    index = CodeIndex(
        project=target.substrate, data_dir=data_dir, runtime=runtime, embedder=LiveEmbedder()
    )

    started = time.monotonic()
    applied = ""
    try:
        if not verify_only:
            stats = await index.apply_delta(target.changeset)
            applied = f"items={getattr(stats, 'items_upserted', '?')} refs={getattr(stats, 'references_upserted', '?')}"

        endpoints = await runtime.start_for_commit(project_id, target.sha)
        counts = _query_counts(endpoints)
    finally:
        await index.close()
        # A failure stopping the process must not mask the load's own result.
        with contextlib.suppress(Exception):
            await runtime.stop_for_commit(project_id, target.sha)

    elapsed = int(time.monotonic() - started)
    return (
        f"{target.repo:<14} project={project_id} sha={target.sha[:7]} "
        f"{applied + ' ' if applied else ''}graph: {counts} ({elapsed}s)"
    )


def _query_counts(endpoints) -> str:
    """Count what actually landed in the graph, through the driver the executor uses."""
    from neo4j import GraphDatabase

    password = Path(endpoints.password_file).read_text(encoding="utf-8").strip()
    driver = GraphDatabase.driver(endpoints.bolt_uri, auth=(endpoints.user, password))
    try:
        with driver.session(default_access_mode="READ") as session:
            labels = session.run(
                "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC LIMIT 5"
            ).data()
            rels = session.run(
                "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC LIMIT 5"
            ).data()
    finally:
        driver.close()
    node_text = ", ".join(f"{row['label']}={row['n']}" for row in labels) or "no nodes"
    rel_text = ", ".join(f"{row['rel']}={row['n']}" for row in rels) or "no edges"
    return f"[{node_text}] [{rel_text}]"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changesets", type=Path, default=DEFAULT_CHANGESETS)
    parser.add_argument("--substrates", type=Path, default=DEFAULT_SUBSTRATES)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--only", default=None, help="load one repository by name")
    parser.add_argument(
        "--grace",
        type=float,
        default=120.0,
        help="seconds to let Neo4j shut down cleanly; too short and the load is discarded",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="check keys and report what is already in the graph; apply nothing",
    )
    args = parser.parse_args()

    targets = _targets(args.changesets, args.substrates, args.only)
    if not targets:
        print("no targets — check --changesets and --only", file=sys.stderr)
        return 2

    failures = 0
    for target in targets:
        reason = _check(target)
        if reason is not None:
            failures += 1
            print(f"{target.repo:<14} REFUSED — {reason}")
            continue
        try:
            print(await _load(target, args.data_dir, args.verify_only, args.grace), flush=True)
        except Exception as exc:  # noqa: BLE001 - one repo's failure must not stop the rest
            failures += 1
            print(f"{target.repo:<14} FAILED — {type(exc).__name__}: {exc}"[:300], flush=True)

    print(f"\n{len(targets) - failures}/{len(targets)} loaded")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
