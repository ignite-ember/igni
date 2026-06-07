"""Config loader — reads JSON from disk with a default timeout."""

import json
from pathlib import Path


def load(path: str, timeout: int = 30) -> dict:
    return json.loads(Path(path).read_text())
