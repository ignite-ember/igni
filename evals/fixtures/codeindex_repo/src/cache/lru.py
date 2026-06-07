"""A clean LRU cache implementation — the gold-standard fixture.

Tagged ``quality=excellent``, ``testing=well-tested``, ``patterns=[lru]``,
``complexity=low``. Used by ``auth/session.py``.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


class LRUCache:
    """Least-recently-used cache with a fixed capacity.

    Pure ``OrderedDict`` implementation — no external deps, predictable
    big-O, easy to test.
    """

    def __init__(self, maxsize: int = 128) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._data: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: str, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def evict(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)
