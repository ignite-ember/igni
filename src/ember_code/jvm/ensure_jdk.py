"""Ensure a JDK is available for Neo4j.

This is the plugin-facing thin wrapper. All three clients (Tauri, JetBrains, VSCode)
call through here so the bootstrap logic lives in one place.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ember_code.backend.jdk_bootstrap import JdkBootstrap

# Lazily-created bootstrap instance (created on first ensure_jdk call).
_bootstrap: JdkBootstrap | None = None


def ensure_jdk(data_dir: str | Path, *, version: str = "21") -> Path:
    """Ensure JDK is available and return the JAVA_HOME-equivalent path.

    This is a sync wrapper around :meth:`JdkBootstrap.ensure` for clients
    (Tauri, JetBrains, VSCode) that invoke it as a subprocess rather than
    importing the Python module directly. Uses :func:`asyncio.run` internally
    so callers don't need an event loop.

    Returns:
        Path to the JDK root (the directory containing ``bin/java``).

    Raises:
        SystemExit: if the JDK cannot be installed after two attempts.
    """
    global _bootstrap
    cache_root = Path(data_dir) / "jdk"

    if _bootstrap is None:
        _bootstrap = JdkBootstrap(version=version, cache_root=cache_root)

    try:
        return asyncio.run(_bootstrap.ensure())
    except Exception as exc:
        raise SystemExit(f"failed to bootstrap JDK: {exc}") from exc


if __name__ == "__main__":
    # CLI entry point: python -m ember_code.jvm.ensure_jdk <data_dir>
    import argparse

    parser = argparse.ArgumentParser(description="Ensure JDK is available")
    parser.add_argument("data_dir", help="Path to data directory")
    parser.add_argument("--version", default="21")
    args = parser.parse_args()

    try:
        java_home = ensure_jdk(args.data_dir, version=args.version)
        print(java_home)
        sys.exit(0)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
