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

    python scripts/reindex_hnsw.py /path/to/target/project [--data-dir ~/.ember]

Run against the eval-target project to rebuild its index. Per-commit
chroma directories are walked under
``<data-dir>/projects/<repo-id>/code_index/<sha>/`` so a single run
upgrades every commit's collections.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

# Make ember_code importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ember_code.core.code_index.paths import commit_chroma_path  # noqa: E402
from ember_code.core.embeddings import EmbeddingFunction  # noqa: E402

# Mirror what _get_or_create_collection now sets — keep these in sync.
TARGET_HNSW_METADATA = {
    "hnsw:space": "cosine",
    "hnsw:M": 32,
    "hnsw:construction_ef": 400,
    "hnsw:search_ef": 10000,
}

COLLECTION_NAMES = ("code_index_documents", "code_index_chunks")
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


def reindex_one_commit(chroma_dir: Path) -> tuple[int, int]:
    """Rebuild both collections under one commit's chroma dir.

    Returns (docs_count, chunks_count) copied.
    """
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_dir))
    counts: list[int] = []
    for name in COLLECTION_NAMES:
        try:
            n = _copy_collection(client, name)
        except ValueError:
            print(f"  [{name}] not present, skipping")
            n = 0
        counts.append(n)
    return tuple(counts)  # type: ignore[return-value]


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
    args = parser.parse_args()

    project = Path(args.project_dir).resolve()
    data_dir = Path(args.data_dir).expanduser()
    if not project.exists():
        print(f"Project not found: {project}", file=sys.stderr)
        return 2

    # Find all commits that have a chroma dir for this project.
    # commit_chroma_path's parent (per-project) holds one subdir per sha.
    sample_path = commit_chroma_path(project, "PROBE", data_dir=data_dir)
    code_index_root = sample_path.parent
    if not code_index_root.exists():
        print(f"No code_index dirs at {code_index_root}", file=sys.stderr)
        return 1

    commit_dirs = [
        p
        for p in code_index_root.iterdir()
        if p.is_dir() and p.suffix == ".chroma" and not p.name.startswith("PROBE")
    ]
    if not commit_dirs:
        print(f"No commit dirs in {code_index_root}", file=sys.stderr)
        return 1

    print(f"Rebuilding {len(commit_dirs)} commit(s) under {code_index_root}")
    grand_total = 0
    t0 = time.monotonic()
    for chroma_dir in sorted(commit_dirs):
        sha = chroma_dir.name
        print(f"\n=== {sha[:12]} ({chroma_dir}) ===")
        if args.keep_backup:
            bak = chroma_dir.with_suffix(".bak")
            if bak.exists():
                shutil.rmtree(bak)
            shutil.copytree(chroma_dir, bak)
            print(f"  backup: {bak}")
        try:
            docs_n, chunks_n = reindex_one_commit(chroma_dir)
            print(f"  done: {docs_n} docs, {chunks_n} chunks")
            grand_total += docs_n + chunks_n
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1

    elapsed = time.monotonic() - t0
    print(
        f"\nReindex complete: {grand_total} items across "
        f"{len(commit_dirs)} commit(s) in {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
