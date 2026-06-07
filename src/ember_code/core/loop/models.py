"""Pydantic domain model for the persisted ``/loop`` state.

Kept separate from ``db_models.py`` (SQLAlchemy) so importing the
domain type doesn't drag in SQLAlchemy. Mirrors the three runtime
fields on :class:`Session` plus the ``run_id`` that scopes
:class:`LoopProgressStore` entries to the current loop.
"""

from __future__ import annotations

from pydantic import BaseModel


class LoopState(BaseModel):
    """Snapshot of the active ``/loop``.

    ``run_id`` is a uuid4 minted when the loop starts; it scopes
    every :class:`LoopProgressStore` entry so a fresh loop doesn't
    accidentally inherit the previous run's progress rows.
    ``iteration_index`` is the 1-based iteration currently in
    flight; ``iterations_remaining`` is how many more will run
    after the current one. Sum is the *current* safety bound but
    not necessarily the *intended* total (see ``cap_explicit``).

    ``cap_explicit`` distinguishes:

    * ``True``  → the user typed ``/loop N <prompt>`` (or the
      agent passed an explicit ``max_iterations``). ``N`` is both
      the safety bound AND the intended total; we terminate when
      the counter hits it and display ``N / M`` in the panel.
    * ``False`` → ``/loop <prompt>`` with no leading number. The
      cap is just a runaway safety net; on cap-hit we auto-extend
      by another batch and keep going (until ``LOOP_HARD_CAP``).
      The panel hides the "total" since there isn't one — just
      shows the current iteration.
    """

    run_id: str
    prompt: str
    iteration_index: int
    iterations_remaining: int
    cap_explicit: bool = False
