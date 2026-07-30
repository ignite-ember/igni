"""Todos-snapshot store — persists the live ``TodoStore`` state.

Composes :class:`SessionDataService`; coercion runs via
:meth:`TodoItemWire.coerce_snapshot` so the wire-shape rule
(non-empty ``content``, valid ``status``, ``active_form``
defaults) lives on the model that defines the shape rather than
in a module-level free helper.

The store dumps ``by_alias=True`` at the SQL boundary so the
persisted rows keep the camelCase ``activeForm`` key parity
with the ``todo_write`` broadcast payload.
"""

from __future__ import annotations

from ember_code.core.session.persistence.data_service import SessionDataService
from ember_code.core.session.schemas import LoadResult, PersistResult
from ember_code.core.tools.todo import TodoItemWire


class TodoSnapshotStore:
    """Coordinator for the ``todos`` sub-blob of ``session_data``."""

    KEY = "todos"

    def __init__(self, data_service: SessionDataService) -> None:
        self._data = data_service

    async def load(self) -> LoadResult[list[TodoItemWire]]:
        """Read the persisted todo snapshot.

        Coercion runs via :meth:`TodoItemWire.coerce_snapshot` so
        malformed rows drop silently — callers fall back to the
        plan's original task list, which is at least consistent
        with the user's last hand-approved state.
        """
        return await self._data.read_key(self.KEY, TodoItemWire.coerce_snapshot)

    async def save(self, todos: list[TodoItemWire]) -> PersistResult:
        """Atomic-replace the persisted todo snapshot.

        Each :class:`TodoItemWire` dumps via ``by_alias=True`` so
        the on-disk shape matches the ``todos_updated`` broadcast
        payload verbatim.
        """
        cleaned = [t.model_dump(by_alias=True) for t in todos]
        return await self._data.write_key(self.KEY, cleaned)
