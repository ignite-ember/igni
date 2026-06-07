"""Domain schema for the code_index package."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID


def convert_weaviate_types(data: Any) -> Any:
    """Recursively convert Weaviate-returned datetime/UUID values to strings."""
    if isinstance(data, datetime):
        return data.isoformat()
    if isinstance(data, UUID):
        return str(data)
    if isinstance(data, dict):
        return {k: convert_weaviate_types(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_weaviate_types(item) for item in data]
    return data


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()
