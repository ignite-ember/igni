"""Configuration parser used across the app."""

import json
from pathlib import Path


def parse_config(path: str) -> dict:
    return json.loads(Path(path).read_text())


def parse_int(value: str) -> int:
    return int(value)
