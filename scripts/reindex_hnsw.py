"""Rebuild existing chroma collections with high-recall HNSW config.

Chroma's default HNSW parameters (``search_ef=10``, ``M=16``) give
broken recall at our scale — top-K queries with K > 10 return
near-floor noise instead of the actually-closest neighbors. The fix
is collection metadata at creation time, but existing collections
were created with defaults and there's no in-place reconfigure.

This script copies each existing chroma collection to a fresh one
with the high-recall config, then swaps. No re-summarization or
re-embedding — we're moving already-computed vectors.

Usage::

    # Default: rebuild both code_index (per-commit) and knowledge.
    python scripts/reindex_hnsw.py /path/to/target/project [--data-dir ~/.ember]

    # Limit to one scope.
    python scripts/reindex_hnsw.py /path/to/target/project --scope code_index
    python scripts/reindex_hnsw.py /path/to/target/project --scope knowledge

Code-index chroma dirs are walked under
``<data-dir>/projects/<repo-id>/code_index/<sha>.chroma/`` so a
single run upgrades every commit's collections. The knowledge dir
lives at ``<data-dir>/projects/<repo-id>/knowledge.chroma/`` and is
processed in the same pass.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# Make ember_code importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ember_code.core.code_index.chroma_client_factory import ChromaClientFactory  # noqa: E402
from ember_code.core.code_index.paths import (  # noqa: E402
    commit_chroma_path,
    knowledge_chroma_path,
)
from ember_code.core.embeddings import EmbeddingFunction  # noqa: E402

# Single source of truth — bump ``ChromaClientFactory.HNSW_METADATA``
# and both the runtime and this rebuild script pick up the new values.
TARGET_HNSW_METADATA = dict(ChromaClientFactory.HNSW_METADATA)

CODE_INDEX_COLLECTION_NAMES = ("code_index_documents", "code_index_chunks")
KNOWLEDGE_COLLECTION_NAMES = ("knowledge_documents", "knowledge_chunks")
BATCH_SIZE = 500


def _copy_collection(client: object, src_name: str) -> int:
    """Copy a collection to a temp name with new HNSW metadata, then swap.

    Returns the number of items copied. Raises on failure — caller
    should leave the source intact and surface the error.
    """
    # Pass the same EmbeddingFunction the runtime uses so the
    # destination collection inherits the right ``ember-sentence-transformer``
    # name, not chroma's "default" placeholder.
    ef = EmbeddingFunction()
    src = client.get_collection(name=src_name, embedding_function=ef)  # type: ignore[attr-defined]
    total = src.count()
    if total == 0:
        print(f"  [{src_name}] empty, skipping")
        return 0

    # Drop any leftover temp collection from a previous failed run.
    tmp_name = f"{src_name}__rehnsw_tmp"
    try:
        client.delete_collection(name=tmp_name)  # type: ignore[attr-defined]
    except Exception:
        pass

    dst = client.create_collection(  # type: ignore[attr-defined]
        name=tmp_name,
        embedding_function=ef,
        metadata=TARGET_HNSW_METADATA,
    )

    # Pull and insert in batches. We need ``include`` to bring the
    # vectors over verbatim (otherwise the destination would re-embed
    # via the embedding_function, which is the heavy step we want to
    # skip).
    copied = 0
    for offset in range(0, total, BATCH_SIZE):
        page = src.get(
            limit=BATCH_SIZE,
            offset=offset,
            include=["embeddings", "documents", "metadatas"],
        )
        ids = page.get("ids") or []
        if not ids:
            break
        dst.add(
            ids=ids,
            embeddings=page.get("embeddings"),
            documents=page.get("documents"),
            metadatas=page.get("metadatas"),
        )
        copied += len(ids)
        print(f"  [{src_name}] copied {copied}/{total}", flush=True)

    # Sanity check
    if dst.count() != total:
        raise RuntimeError(
            f"copy verify failed for {src_name}: src={total} dst={dst.count()}"
        )

    # Swap: drop original, rename temp into its place.
    client.delete_collection(name=src_name)  # type: ignore[attr-defined]
    # chroma doesn't support rename — recreate at the original name and
    # copy the temp's data over. (Yes, second copy. Fast for in-memory
    # data; chroma reads embeddings back from sqlite either way.)
    final = client.create_collection(  # type: ignore[attr-defined]
        name=src_name,
        embedding_function=ef,
        metadata=TARGET_HNSW_METADATA,
    )
    for offset in range(0, total, BATCH_SIZE):
        page = dst.get(
            limit=BATCH_SIZE,
            offset=offset,
            include=["embeddings", "documents", "metadatas"],
        )
        ids = page.get("ids") or []
        if not ids:
            break
        final.add(
            ids=ids,
            embeddings=page.get("embeddings"),
            documents=page.get("documents"),
            metadatas=page.get("metadatas"),
        )

    if final.count() != total:
        raise RuntimeError(
            f"swap verify failed for {src_name}: temp={total} final={final.count()}"
        )

    client.delete_collection(name=tmp_name)  # type: ignore[attr-defined]
    return total


def reindex_chroma_dir(chroma_dir: Path, collection_names: tuple[str, ...]) -> tuple[int, int]:
    """Rebuild a chroma dir's named collections.

    Returns (docs_count, chunks_count) copied — generic over which
    pair of collections we're rebuilding (code-index or knowledge).
    """
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_dir))
    counts: list[int] = []
    for name in collection_names:
        try:
            n = _copy_collection(client, name)
        except ValueError:
            print(f"  [{name}] not present, skipping")
            n = 0
        counts.append(n)
    return tuple(counts)  # type: ignore[return-value]


def _reindex_code_index(project: Path, data_dir: Path, keep_backup: bool) -> tuple[int, int]:
    """Walk every commit's chroma dir for ``project`` and rebuild. Returns (dirs, items)."""
    sample_path = commit_chroma_path(project, "PROBE", data_dir=data_dir)
    code_index_root = sample_path.parent
    if not code_index_root.exists():
        print(f"[code_index] no dirs at {code_index_root}, skipping")
        return (0, 0)

    commit_dirs = [
        p
        for p in code_index_root.iterdir()
        if p.is_dir() and p.suffix == ".chroma" and not p.name.startswith("PROBE")
    ]
    if not commit_dirs:
        print(f"[code_index] no commit dirs in {code_index_root}, skipping")
        return (0, 0)

    print(f"\n[code_index] rebuilding {len(commit_dirs)} commit(s) under {code_index_root}")
    items = 0
    for chroma_dir in sorted(commit_dirs):
        sha = chroma_dir.name
        print(f"\n=== code_index {sha[:12]} ({chroma_dir}) ===")
        if keep_backup:
            bak = chroma_dir.with_suffix(".bak")
            if bak.exists():
                shutil.rmtree(bak)
            shutil.copytree(chroma_dir, bak)
            print(f"  backup: {bak}")
        docs_n, chunks_n = reindex_chroma_dir(chroma_dir, CODE_INDEX_COLLECTION_NAMES)
        print(f"  done: {docs_n} docs, {chunks_n} chunks")
        items += docs_n + chunks_n
    return (len(commit_dirs), items)


def _reindex_knowledge(project: Path, data_dir: Path, keep_backup: bool) -> tuple[int, int]:
    """Rebuild the single knowledge chroma dir for ``project``. Returns (dirs, items)."""
    chroma_dir = knowledge_chroma_path(project, data_dir=data_dir)
    if not chroma_dir.exists():
        print(f"[knowledge] no dir at {chroma_dir}, skipping")
        return (0, 0)

    print(f"\n[knowledge] rebuilding {chroma_dir}")
    if keep_backup:
        bak = chroma_dir.with_suffix(".bak")
        if bak.exists():
            shutil.rmtree(bak)
        shutil.copytree(chroma_dir, bak)
        print(f"  backup: {bak}")
    docs_n, chunks_n = reindex_chroma_dir(chroma_dir, KNOWLEDGE_COLLECTION_NAMES)
    print(f"  done: {docs_n} docs, {chunks_n} chunks")
    return (1, docs_n + chunks_n)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", help="The project whose index to rebuild")
    parser.add_argument(
        "--data-dir", default="~/.ember", help="Ember data dir (default: ~/.ember)"
    )
    parser.add_argument(
        "--keep-backup",
        action="store_true",
        help="Move old chroma dirs to .bak before rebuilding (default: in-place)",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "code_index", "knowledge"),
        default="all",
        help="Which index to rebuild (default: all)",
    )
    args = parser.parse_args()

    project = Path(args.project_dir).resolve()
    data_dir = Path(args.data_dir).expanduser()
    if not project.exists():
        print(f"Project not found: {project}", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    dirs_total = 0
    items_total = 0
    try:
        if args.scope in ("all", "code_index"):
            d, i = _reindex_code_index(project, data_dir, args.keep_backup)
            dirs_total += d
            items_total += i
        if args.scope in ("all", "knowledge"):
            d, i = _reindex_knowledge(project, data_dir, args.keep_backup)
            dirs_total += d
            items_total += i
    except Exception as e:
        print(f"  FAILED: {e}", file=sys.stderr)
        return 1

    if dirs_total == 0:
        print("Nothing to rebuild.", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - t0
    print(
        f"\nReindex complete: {items_total} items across "
        f"{dirs_total} dir(s) in {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
