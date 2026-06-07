"""Shared API key resolution logic.

Centralises the ``api_key`` / ``api_key_env`` / ``api_key_cmd`` look-up
used by ``ModelRegistry``.
"""

import os
import shlex
import subprocess
from typing import Any


def resolve_api_key(entry: dict[str, Any]) -> str | None:
    """Resolve an API key from a registry entry.

    Resolution order:
    1. Literal ``api_key`` value in the entry.
    2. Environment variable named by ``api_key_env``.
    3. Shell command specified by ``api_key_cmd``.

    Returns ``None`` if no key can be resolved.
    """
    if "api_key" in entry:
        return entry["api_key"]
    if "api_key_env" in entry:
        key = os.environ.get(entry["api_key_env"])
        if key:
            return key
    if "api_key_cmd" in entry:
        result = subprocess.run(shlex.split(entry["api_key_cmd"]), capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None
