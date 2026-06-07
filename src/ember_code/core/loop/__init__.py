"""Per-project SQLite persistence for ``/loop``.

Two pieces share this module:

* :class:`LoopStore` — the active ``/loop``'s session state
  (prompt + iteration counters + run_id), persisted to a single
  ``loop_state`` row so it survives a CLI restart.
* :class:`LoopProgressStore` — a per-run key/value scratchpad
  the model writes to during iterations to track section-by-
  section progress without re-doing completed work on the next
  iteration.

Both stores live in the same ``state.db`` the scheduler uses —
one per-project SQLite file so switching projects doesn't expose
another project's loop state.
"""

from ember_code.core.loop.limits import (
    LOOP_DEFAULT_MAX_ITERATIONS,
    LOOP_HARD_CAP,
)
from ember_code.core.loop.models import LoopState
from ember_code.core.loop.prompt import wrap_iteration_prompt
from ember_code.core.loop.store import LoopProgressStore, LoopStore

__all__ = [
    "LOOP_DEFAULT_MAX_ITERATIONS",
    "LOOP_HARD_CAP",
    "LoopProgressStore",
    "LoopState",
    "LoopStore",
    "wrap_iteration_prompt",
]
