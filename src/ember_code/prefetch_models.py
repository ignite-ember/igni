"""One-shot model prefetcher used by the IDE plugins' bootstrap.

Calling ``python -m ember_code.prefetch_models`` warms the
sentence-transformer cache so the first agent run doesn't stall
mid-conversation while a 90 MB embedding model downloads silently.
The IDE plugins' managed-runtime bootstrap (``EmberRuntime.kt`` for
JetBrains, ``runtime.ts`` for VSCode) calls this after installing
``ignite-ember`` so the user's loading screen accounts for it —
the model download moves from "agent hung for 30 seconds" to
"plugin doing its visible install".

Idempotent: ``get_model()`` returns the cached model on subsequent
calls and ``SentenceTransformer`` itself skips the network probe when
``HF_HUB_OFFLINE=1`` is set (which our embeddings loader sets
automatically when the cache already exists — see
``core/embeddings.py``).

Output:
  * Progress lines go to stdout (one per stage) so the plugin
    bootstrap can surface them. Errors go to stderr and exit non-zero
    so the bootstrap knows to surface the failure.

Environment:
  * ``HF_HOME`` — honored by sentence-transformers/transformers.
    The plugin sets this to a path inside the managed cache so the
    Reinstall Backend (Clean) action really wipes everything.
"""

from __future__ import annotations

import sys

from ember_code.core.embeddings import DEFAULT_MODEL, get_model


def main() -> int:
    print("prefetch: loading sentence-transformer (~90 MB on first run)", flush=True)
    try:
        model = get_model()
    except Exception as exc:  # pragma: no cover — surfaced to caller
        print(f"prefetch: failed: {exc!r}", file=sys.stderr, flush=True)
        return 1

    # ``encode`` a trivial input so we exercise the full path (tokenizer
    # + model forward + numpy round-trip). If anything is wrong with the
    # cached weights or platform-specific torch wheel, the failure
    # surfaces here instead of during the user's first chat turn.
    try:
        _ = model.encode(["ember"], show_progress_bar=False, convert_to_numpy=True)
    except Exception as exc:  # pragma: no cover
        print(f"prefetch: warm-up encode failed: {exc!r}", file=sys.stderr, flush=True)
        return 2

    print(f"prefetch: {DEFAULT_MODEL} ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
