"""Regenerate both SDKs' `host` objects from igni's host schema.

    python scripts/generate_host_sdks.py          # write what changed
    python scripts/generate_host_sdks.py --check  # exit 1 if anything is stale

The schema is `ember_code.extensions.host.definition`; the generators are
`ember_code.extensions.host.codegen`. `make host-sdks` runs the first form.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ember_code.extensions.host.codegen import HostSdks

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    sdks = HostSdks(ROOT)
    if "--check" in argv:
        stale = sdks.stale()
        for path in stale:
            print(f"stale: {path.relative_to(ROOT)}")
        return 1 if stale else 0
    for path in sdks.write():
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
