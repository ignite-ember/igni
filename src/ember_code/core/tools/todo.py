"""TodoWrite — agent-facing planning tool, Claude Code parity.

The agent uses ``todo_write`` to maintain a per-session list of
upcoming / in-progress / completed work items. Each call REPLACES
the entire list (atomic snapshot semantics — matches CC's
``TodoWrite``), so the agent ships the full intended state on
every call instead of issuing per-item updates.

Why a tool at all (vs. having the agent narrate its plan in the
conversation):

* The user gets a stable, structured view of intent that survives
  intermediate tool calls and reasoning — no scrolling through
  output to find "what was the plan again?".
* The list is consumable by external surfaces (a webview sidebar,
  the TUI status line) via the ``GET_TODOS`` RPC without parsing
  prose.
* The model is steered to **commit** to a plan and tick it off
  one step at a time, instead of dropping intermediate planning
  noise into every turn.

The single rule the model needs to follow: keep at most one item
in ``in_progress`` at a time, and flip a completed item before
starting the next. That property isn't enforced here (we record
whatever the model writes) — it's encouraged by the docstring
and surfaced in the tool's reply summary so the model can self-
correct.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agno.tools import Toolkit
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


TodoStatus = Literal["pending", "in_progress", "completed"]
_VALID_STATUSES: frozenset[str] = frozenset(("pending", "in_progress", "completed"))


class TodoItemWire(BaseModel):
    """Wire shape for one todo row — CC-parity ``activeForm``
    camelCase alias so the SDK-facing payload matches Claude Code
    verbatim. Constructed via keyword-args with the Python-side
    ``active_form`` snake case; ``.model_dump(by_alias=True)``
    produces the camelCase dict for broadcasts / persistence."""

    model_config = ConfigDict(populate_by_name=True)

    content: str
    status: str
    active_form: str = Field("", alias="activeForm")


@dataclass(frozen=True)
class TodoItem:
    """One row in the session's todo list.

    ``activeForm`` is the verb-noun gerund the UI shows while the
    item is ``in_progress`` — e.g. ``"Running tests"`` (active)
    paired with ``content`` ``"Run tests"`` (imperative). Two
    fields, not one, so the UI can render the active form
    distinctly without re-conjugating verbs at display time.
    """

    content: str
    status: TodoStatus
    active_form: str = ""


@dataclass
class TodoStore:
    """Per-session todo list. Plain list under the hood, but the
    accessors (``set`` / ``items``) live here so future work
    (event emission, persistence) has a single chokepoint."""

    items: list[TodoItem] = field(default_factory=list)

    def set(self, items: list[TodoItem]) -> None:
        # Replace atomically — the contract is "the list IS now
        # this". No partial merge, no delta detection.
        self.items = list(items)

    def snapshot(self) -> list[dict]:
        """Serialise to the wire shape (``activeForm`` camelCase,
        matching how CC's tool payload looks on the SDK). The
        shape is defined once by :class:`TodoItemWire`; the
        list-comp constructs and dumps via ``by_alias=True`` so
        any future field addition/rename goes through the model,
        not a hand-rolled dict."""
        return [
            TodoItemWire(
                content=item.content,
                status=item.status,
                active_form=item.active_form,
            ).model_dump(by_alias=True)
            for item in self.items
        ]


def _coerce_items(raw: list) -> tuple[list[TodoItem], list[str]]:
    """Validate and convert the agent-supplied list. Returns
    ``(items, errors)``. Errors are surfaced in the tool's reply
    so the model can correct on the next call rather than
    silently dropping malformed rows."""
    items: list[TodoItem] = []
    errors: list[str] = []
    if not isinstance(raw, list):
        return [], ["todos must be a list"]
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"todos[{idx}]: not a dict")
            continue
        content = str(entry.get("content", "")).strip()
        if not content:
            errors.append(f"todos[{idx}]: empty content")
            continue
        status = str(entry.get("status", "pending")).strip().lower()
        if status not in _VALID_STATUSES:
            errors.append(f"todos[{idx}]: status {status!r} not in {sorted(_VALID_STATUSES)}")
            continue
        # Accept both camelCase (CC parity) and snake_case keys.
        active_form = str(entry.get("activeForm", entry.get("active_form", "")) or "").strip()
        items.append(TodoItem(content=content, status=status, active_form=active_form))
    return items, errors


class TodoTools(Toolkit):
    """Single-method toolkit: ``todo_write``."""

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_todo")
        self._session = session
        self.register(self.todo_write)

    def _broadcast_state(self) -> None:
        """Push the current ``TodoStore`` snapshot on the
        ``todos_updated`` channel so attached clients (the
        PlanCard checklist, a future todos panel) re-render
        with the latest statuses. Best-effort — sessions
        without a wired transport (tests, headless) just no-op
        via the empty broadcast list."""
        broadcast = getattr(self._session, "broadcast", None)
        store = getattr(self._session, "todo_store", None)
        if broadcast is None or store is None:
            return
        broadcast("todos_updated", {"todos": store.snapshot()})

    async def _persist_state(self) -> None:
        """Write the current ``TodoStore`` snapshot to
        ``session_data["todos"]`` so the live execution state
        (in_progress / completed flips) survives BE restart.

        Without this, ``_rehydrate_plan_store`` falls back to
        the plan's original ``exit_plan_mode(tasks=...)`` list —
        everything pending, all progress erased. Best-effort:
        no persistence layer / DB error → silent skip (the
        in-memory state and ``todos_updated`` broadcast still
        reach attached clients).
        """
        persistence = getattr(self._session, "persistence", None)
        store = getattr(self._session, "todo_store", None)
        if persistence is None or store is None:
            return
        try:
            await persistence.save_todos(store.snapshot())
        except Exception as exc:
            logger.debug("todo persist failed: %s", exc)

    async def todo_write(self, todos: list) -> str:
        """Replace the session's todo list with ``todos``.

        Each item is a dict with three fields:

        * ``content`` — imperative description (e.g. ``"Run
          tests"``). Required, non-empty.
        * ``status`` — ``"pending"``, ``"in_progress"``, or
          ``"completed"``. Required.
        * ``activeForm`` — verb-noun gerund to display while the
          item is in progress (e.g. ``"Running tests"``).
          Optional.

        The list is replaced atomically — pass the FULL intended
        state on every call. Mark items ``completed`` as soon as
        the work is done, not in batches at the end; keep at most
        one item ``in_progress``.

        Returns a short summary so the model gets immediate
        feedback ("3 todos: 1 completed, 1 in_progress, 1
        pending") and surfaces any validation errors from the
        payload.
        """
        items, errors = _coerce_items(todos)
        if not errors and not items:
            # Empty list IS valid (clears the plan) but we want
            # a positive confirmation so the model knows the call
            # took effect.
            self._session.todo_store.set([])
            self._broadcast_state()
            await self._persist_state()
            return "Cleared todo list."
        if items:
            self._session.todo_store.set(items)
            self._broadcast_state()
            await self._persist_state()
        counts = {s: 0 for s in _VALID_STATUSES}
        for item in items:
            counts[item.status] += 1
        msg = (
            f"{len(items)} todos: "
            f"{counts['completed']} completed, "
            f"{counts['in_progress']} in_progress, "
            f"{counts['pending']} pending."
        )
        if errors:
            joined = "; ".join(errors)
            msg += f" Validation errors (ignored): {joined}"
        if counts["in_progress"] > 1:
            msg += (
                " Note: keep at most one item in_progress at a time —"
                " mark the previous in_progress item completed before"
                " starting another."
            )
        return msg
