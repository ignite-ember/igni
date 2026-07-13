"""``/loop`` state helpers for :class:`Session`.

Extracted from ``session/core.py`` so the god-file has fewer
top-level concerns. Each function takes the session as an
explicit argument and mutates ``session.loop_*`` fields plus
``session.loop_store``. Session keeps thin method wrappers
(``Session.start_loop`` etc.) so existing call sites — the
``/loop`` slash command, ``LoopTools.loop_*``, the run
controller's ``_check_loop_continuation`` — see no signature
change.

The state model:

* ``pending_loop_prompt`` — active iteration text, or ``None``
  when no loop is running.
* ``loop_run_id`` — UUID scoping :class:`LoopProgressStore`
  writes.
* ``loop_iteration_index`` / ``loop_iterations_remaining`` —
  1-based counter of iterations dispatched + safety-net budget.
* ``loop_cap_explicit`` — whether ``max_iter`` is the user's
  intended total (terminate at cap) or a rolling safety net
  (auto-extend at cap, stop only at ``LOOP_HARD_CAP``).
* ``loop_paused`` — flipped by ``advance_loop`` when an
  implicit loop hits ``LOOP_HARD_CAP``, or by the run
  controller when an iteration's ``_run`` raises. Cleared by
  ``resume_loop`` or a fresh ``start_loop``.
* ``_auto_extended_this_advance`` — one-shot flag consumed by
  ``advance_loop`` to signal the FE that the safety net just
  rolled over.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember_code.core.loop.limits import LOOP_DEFAULT_MAX_ITERATIONS, LOOP_HARD_CAP
from ember_code.core.loop.prompt import wrap_iteration_prompt
from ember_code.core.loop.store import LoopState

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


class LoopAdvance(BaseModel):
    """Wire shape for :func:`advance_loop`.

    Three effective states, discriminated by two boolean flags:

    * ``completed=True`` (explicit cap hit) — ``total_iterations``
      carries the final count; other fields empty.
    * ``safety_cap_paused=True`` (implicit hit ``LOOP_HARD_CAP``) —
      ``iteration`` carries the count reached; the loop is now
      paused (user must ``/loop resume`` to continue).
    * both flags False — the normal advance: ``prompt`` /
      ``display_prompt`` / ``iteration`` / ``remaining`` /
      ``cap_explicit`` populated. ``auto_extended`` is a one-shot
      flag when the implicit-cap safety net just rolled over.

    ``advance_loop`` returns ``None`` (not a model) when no loop
    is active; the return type is ``LoopAdvance | None``.
    """

    completed: bool = False
    total_iterations: int = 0
    safety_cap_paused: bool = False
    prompt: str = ""
    display_prompt: str = ""
    iteration: int = 0
    remaining: int = 0
    cap_explicit: bool = False
    auto_extended: bool = False


async def load_persisted_loop_state(session: "Session") -> None:
    """Hydrate the in-memory loop fields from the ``loop_state`` row.

    Called by ``BackendServer.startup`` after the session is
    constructed. If the CLI was killed mid-loop, this is what
    restores the prompt + counters so the panel shows the
    interrupted state. The loop is left in the *paused* state —
    no iteration fires until the user explicitly resumes.
    Idempotent — safe to call multiple times.
    """
    state = await session.loop_store.load()
    if state is None:
        return
    session.loop_run_id = state.run_id
    session.pending_loop_prompt = state.prompt
    session.loop_iteration_index = state.iteration_index
    session.loop_iterations_remaining = state.iterations_remaining
    session.loop_cap_explicit = state.cap_explicit
    session.loop_paused = True


async def start_loop(
    session: "Session",
    prompt: str,
    max_iter: int,
    *,
    immediate: bool,
    cap_explicit: bool,
) -> str:
    """Mint a new ``/loop`` and persist it.

    ``immediate=True`` is the slash-command path: iteration 1
    fires now via the ``run_prompt`` action, so the counters
    start at ``index=1, remaining=max-1``.

    ``immediate=False`` is the agent-tool path: iteration 1
    fires on the *next* idle cycle via :func:`advance_loop`,
    so counters start at ``index=0, remaining=max``.

    ``cap_explicit`` decides how the counter's zero-out is
    handled: explicit → terminate at the cap; implicit →
    auto-extend the safety-net budget, stop only at
    ``LOOP_HARD_CAP``.

    Returns the freshly-minted ``run_id`` so the caller can
    scope :class:`LoopProgressStore` writes to it.
    """
    session.loop_run_id = str(uuid.uuid4())
    session.pending_loop_prompt = prompt
    session.loop_cap_explicit = cap_explicit
    # A freshly started loop is always pumping — clear any leftover
    # paused flag from a previous restart.
    session.loop_paused = False
    if immediate:
        session.loop_iteration_index = 1
        session.loop_iterations_remaining = max_iter - 1
    else:
        session.loop_iteration_index = 0
        session.loop_iterations_remaining = max_iter
    await _persist_loop_state(session)
    return session.loop_run_id


async def advance_loop(session: "Session") -> LoopAdvance | None:
    """Pop the next iteration descriptor and persist the new counters.

    Returns ``None`` when no loop is active. See :class:`LoopAdvance`
    for the three effective shapes carried by the returned model.
    """
    if session.pending_loop_prompt is None:
        return None
    # Paused loops don't auto-advance — see the module docstring.
    if session.loop_paused:
        return None
    if session.loop_iterations_remaining <= 0:
        # Explicit caps still terminate at the user's N.
        if session.loop_cap_explicit:
            total = session.loop_iteration_index
            await cancel_loop(session)
            return LoopAdvance(completed=True, total_iterations=total)
        # Implicit caps: extend the safety net, or pause at the hard
        # ceiling. Pausing (vs. terminating) at ``LOOP_HARD_CAP``
        # lets the user ``/loop resume`` to keep going past 200.
        if session.loop_iteration_index >= LOOP_HARD_CAP:
            await pause_loop(session)
            return LoopAdvance(
                safety_cap_paused=True,
                iteration=session.loop_iteration_index,
            )
        session.loop_iterations_remaining = min(
            LOOP_DEFAULT_MAX_ITERATIONS,
            LOOP_HARD_CAP - session.loop_iteration_index,
        )
        session._auto_extended_this_advance = True
    # An advance means an iteration is firing — the loop is by
    # definition pumping now, even if it was paused a moment ago.
    session.loop_paused = False
    session.loop_iterations_remaining -= 1
    session.loop_iteration_index += 1
    await _persist_loop_state(session)
    # ``total`` is only meaningful when the user explicitly capped
    # the run; otherwise we send no total to the agent so it
    # doesn't try to pace itself against a fake number.
    cap = (
        session.loop_iteration_index + session.loop_iterations_remaining
        if session.loop_cap_explicit
        else None
    )
    wrapped = wrap_iteration_prompt(session.pending_loop_prompt, session.loop_iteration_index, cap)
    # Consume the one-shot ``_auto_extended_this_advance`` signal —
    # the FE renders an info line when this is True on the next
    # advance so the user knows the safety net just rolled over.
    auto_extended = bool(getattr(session, "_auto_extended_this_advance", False))
    if auto_extended:
        session._auto_extended_this_advance = False
    return LoopAdvance(
        prompt=wrapped,
        display_prompt=session.pending_loop_prompt,
        iteration=session.loop_iteration_index,
        remaining=session.loop_iterations_remaining,
        # The FE uses this to decide whether the "N remaining after
        # this one" half of the iteration banner is meaningful.
        cap_explicit=session.loop_cap_explicit,
        auto_extended=auto_extended,
    )


async def cancel_loop(session: "Session") -> bool:
    """Clear ``/loop`` state both in memory and on disk.

    Returns whether anything was active before the call — callers
    use this to decide whether to surface a "loop cancelled"
    message vs. silently no-op'ing on a stray cancel. Progress
    rows for the cancelled ``run_id`` are *kept* — the user can
    clear them via the agent tool if they want a clean slate.
    """
    if session.pending_loop_prompt is None:
        return False
    session.pending_loop_prompt = None
    session.loop_iteration_index = 0
    session.loop_iterations_remaining = 0
    session.loop_run_id = None
    session.loop_paused = False
    session.loop_cap_explicit = False
    await session.loop_store.clear()
    return True


async def pause_loop(session: "Session") -> bool:
    """Flip the active loop to paused without advancing the counter.

    Two callers:

    * :func:`advance_loop` when an implicit loop hits
      ``LOOP_HARD_CAP`` — instead of terminating, we pause and
      let the user decide via ``/loop resume`` / ``/loop stop``.
    * The FE's ``_check_loop_continuation`` when an iteration's
      ``_run`` raises (429, network, tool failure). Pausing
      without advancing means a subsequent ``/loop resume``
      re-fires the *failed* iteration N.

    Returns False when no loop is active. Idempotent.
    """
    if session.pending_loop_prompt is None:
        return False
    session.loop_paused = True
    await _persist_loop_state(session)
    return True


async def resume_loop(session: "Session") -> str | None:
    """Unpause an interrupted ``/loop`` and return its prompt.

    Returns the persisted prompt so the caller can fire
    ``_run(prompt)`` and re-run the iteration that was in flight
    when the CLI died. Returns ``None`` when there's no loop to
    resume, or when the loop is already pumping (not paused).
    """
    if session.pending_loop_prompt is None:
        return None
    if not session.loop_paused:
        return None
    session.loop_paused = False
    await _persist_loop_state(session)
    # Wrap so the resumed iteration carries the same
    # autonomous-loop instructions every other iteration does.
    cap = (
        session.loop_iteration_index + session.loop_iterations_remaining
        if session.loop_cap_explicit
        else None
    )
    return wrap_iteration_prompt(session.pending_loop_prompt, session.loop_iteration_index, cap)


async def _persist_loop_state(session: "Session") -> None:
    """Write the current loop fields to the ``loop_state`` row.

    Helper called by :func:`start_loop` and :func:`advance_loop`.
    ``cancel_loop`` uses ``loop_store.clear`` directly instead —
    saving an "empty" state would leave a stale row around.
    """
    if session.pending_loop_prompt is None or session.loop_run_id is None:
        return
    await session.loop_store.save(
        LoopState(
            run_id=session.loop_run_id,
            prompt=session.pending_loop_prompt,
            iteration_index=session.loop_iteration_index,
            iterations_remaining=session.loop_iterations_remaining,
            cap_explicit=session.loop_cap_explicit,
        )
    )
