"""One-shot model prefetcher used by the clients' managed-runtime bootstrap.

Calling ``python -m ember_code.prefetch_models`` fetches Laya, igni's
decision model (:mod:`ember_code.core.decisions`), so the first Bypass
check or ``host.decide`` does not stall on a ~1.7 GB download. The
bootstraps (Tauri's ``runtime.rs``, ``EmberRuntime.kt`` for JetBrains,
``runtime.ts`` for VS Code) call this after installing igni, so the
download is a visible install step rather than a hung first call.

It is the only model igni has. An extension that needs one ships and
fetches it itself.

Output:
  * Progress lines go to stdout so the bootstrap can surface them.
    A failure goes to stderr and is not fatal: Laya loads on first use
    instead, and the Bypass guard asks rather than allows if it cannot.

Environment:
  * ``HF_HOME`` — honoured by transformers. The plugins set it inside
    the managed cache so a clean reinstall really wipes everything.
"""

from __future__ import annotations

import sys

from ember_code.core.decisions.laya import Laya


def main() -> int:
    print("prefetch: loading igni's decision model (~1.7 GB on first run)", flush=True)
    try:
        Laya.shared()._load()
    except Exception as exc:  # pragma: no cover — surfaced to caller
        print(f"prefetch: decision model not fetched: {exc!r}", file=sys.stderr, flush=True)
        return 0
    print("prefetch: decision model ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
