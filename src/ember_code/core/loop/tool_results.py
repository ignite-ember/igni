"""Structured result envelope for agent-facing ``/loop`` tool calls.

:class:`LoopController` methods that back the ``LoopTools`` toolkit
each return a :class:`LoopToolResult` — the domain layer speaks in
structured outcomes; only the tool adapter (``LoopTools``) turns
the result into an English string via :meth:`LoopToolResult.render`.

Kept in ``core/loop/`` (peer of ``core/session/``) rather than
``core/tools/`` because :class:`LoopController` — which lives in
``core/session/loop_ops.py`` — is the sole constructor site. This
avoids a ``core.session → core.tools`` reverse-dependency; the
tool package imports downward from the loop domain, not the other
way around.

Wire-compatibility note: :meth:`LoopToolResult.render` reproduces
the exact English strings the pre-refactor ``LoopTools`` methods
returned. ``tests/test_loop.py`` asserts on substrings like
``"ERROR"``, ``"no loop"``, ``"12"``, ``"stopped after 3 iteration"``,
``"armed"`` — do not reword the render output without updating
those assertions in lockstep.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class LoopToolErrorCode(StrEnum):
    """Machine-readable outcome codes for structured tool responses.

    ``OK`` sits alongside the error codes so a single
    :class:`LoopToolResult.code` field can carry every outcome —
    non-prose consumers (e.g. a future JSON tool-call transport)
    can branch on ``code`` rather than parsing the English render.
    """

    OK = "ok"
    EMPTY_PROMPT = "empty_prompt"
    NON_POSITIVE_MAX = "non_positive_max"
    EXCEEDS_HARD_CAP = "exceeds_hard_cap"
    ALREADY_ACTIVE = "already_active"
    NO_ACTIVE_LOOP = "no_active_loop"
    NON_POSITIVE_TOTAL = "non_positive_total"
    NOT_PAUSED = "not_paused"
    NO_LOOP_TO_RESUME = "no_loop_to_resume"


class LoopToolResult(BaseModel):
    """Typed outcome of a ``LoopController`` tool-facing method.

    Every ``*_from_tool`` method on :class:`LoopController` returns
    one of these. The tool adapter (``LoopTools``) is the only
    caller that calls :meth:`render` — the domain layer stays
    prose-free.

    Fields (all optional except ``ok`` + ``code`` + ``message``)
    let structured consumers inspect the outcome without parsing:

    * ``iterations_done`` — snapshotted at ``loop_stop``.
    * ``iterations_remaining`` — for ``already_active`` errors.
    * ``iteration_index`` — for status + resume renders.
    * ``max_iterations`` — for ``armed`` renders.
    * ``announced_total`` — for ``set_total`` renders.
    * ``prompt`` — for ``status`` renders.
    """

    ok: bool
    code: LoopToolErrorCode
    message: str
    iterations_done: int | None = None
    iterations_remaining: int | None = None
    iteration_index: int | None = None
    max_iterations: int | None = None
    announced_total: int | None = None
    prompt: str | None = None

    # ── Constructors ─────────────────────────────────────────────

    @classmethod
    def armed(cls, max_iter: int) -> LoopToolResult:
        """``loop_start`` success — first iteration fires next turn."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.OK,
            message=(
                f"Loop armed. Will re-fire this prompt up to {max_iter} "
                "more times. First iteration runs as the next turn."
            ),
            max_iterations=max_iter,
        )

    @classmethod
    def stopped(cls, iterations_done: int) -> LoopToolResult:
        """``loop_stop`` success — active loop cancelled."""
        suffix = "s" if iterations_done != 1 else ""
        return cls(
            ok=True,
            code=LoopToolErrorCode.OK,
            message=f"Loop stopped after {iterations_done} iteration{suffix}.",
            iterations_done=iterations_done,
        )

    @classmethod
    def idle_stop(cls) -> LoopToolResult:
        """``loop_stop`` no-op — nothing was active."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.NO_ACTIVE_LOOP,
            message="No loop is active. Nothing to stop.",
        )

    @classmethod
    def status_idle(cls) -> LoopToolResult:
        """``loop_status`` — no loop is running."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.NO_ACTIVE_LOOP,
            message="No loop is active.",
        )

    @classmethod
    def status_active(
        cls,
        *,
        iteration_index: int,
        iterations_remaining: int,
        prompt: str,
    ) -> LoopToolResult:
        """``loop_status`` — active-loop snapshot."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.OK,
            message=(
                f"Loop active: iteration {iteration_index} done, "
                f"{iterations_remaining} remaining. "
                f"Prompt: {prompt!r}"
            ),
            iteration_index=iteration_index,
            iterations_remaining=iterations_remaining,
            prompt=prompt,
        )

    @classmethod
    def total_announced(cls, total: int) -> LoopToolResult:
        """``loop_set_total`` success."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.OK,
            message=f"Announced loop total: {total} iterations.",
            announced_total=total,
        )

    @classmethod
    def resumed(cls, iterations_done: int) -> LoopToolResult:
        """``loop_resume`` success — loop unpaused."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.OK,
            message=(
                f"Loop unpaused ({iterations_done} done so far). "
                "Next iteration fires after this turn."
            ),
            iterations_done=iterations_done,
        )

    @classmethod
    def resume_noop_running(cls) -> LoopToolResult:
        """``loop_resume`` no-op — loop was already running."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.NOT_PAUSED,
            message="Loop is already running — no resume needed.",
        )

    @classmethod
    def resume_race_no_prompt(cls) -> LoopToolResult:
        """``loop_resume`` raced with another writer — the
        controller returned ``None`` after we entered the resume
        path. Rare: only observed when the loop is unpaused
        between our ``loop_paused`` check and the actual
        ``resume_loop`` call."""
        return cls(
            ok=True,
            code=LoopToolErrorCode.NOT_PAUSED,
            message="Loop is already running.",
        )

    # ── Error constructors ───────────────────────────────────────

    @classmethod
    def error_empty_prompt(cls) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.EMPTY_PROMPT,
            message="ERROR: loop_start needs a non-empty prompt.",
        )

    @classmethod
    def error_non_positive_max(cls) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.NON_POSITIVE_MAX,
            message="ERROR: max_iterations must be positive.",
        )

    @classmethod
    def error_exceeds_hard_cap(cls, max_iterations: int, hard_cap: int) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.EXCEEDS_HARD_CAP,
            message=(
                f"ERROR: max_iterations={max_iterations} exceeds the hard "
                f"cap of {hard_cap}. Pick a smaller number."
            ),
            max_iterations=max_iterations,
        )

    @classmethod
    def error_already_active(
        cls, *, iteration_index: int, iterations_remaining: int
    ) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.ALREADY_ACTIVE,
            message=(
                f"ERROR: a loop is already active "
                f"({iteration_index} done, "
                f"{iterations_remaining} remaining). "
                "Call loop_stop() first if you want to start a new one."
            ),
            iteration_index=iteration_index,
            iterations_remaining=iterations_remaining,
        )

    @classmethod
    def error_no_active_loop(cls) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.NO_ACTIVE_LOOP,
            message="ERROR: no loop is active — call loop_start() first.",
        )

    @classmethod
    def error_non_positive_total(cls) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.NON_POSITIVE_TOTAL,
            message="ERROR: total must be a positive integer.",
        )

    @classmethod
    def error_no_loop_to_resume(cls) -> LoopToolResult:
        return cls(
            ok=False,
            code=LoopToolErrorCode.NO_LOOP_TO_RESUME,
            message="ERROR: no loop to resume.",
        )

    # ── Public API ───────────────────────────────────────────────

    @property
    def is_ok(self) -> bool:
        """Structured success flag — non-prose consumers can branch
        on this without parsing the English render."""
        return self.ok

    def render(self) -> str:
        """Return the agent-visible English string.

        Wire-compatible with the pre-refactor ``LoopTools`` return
        values — do not reword without also updating
        ``tests/test_loop.py``.
        """
        return self.message


__all__ = ["LoopToolErrorCode", "LoopToolResult"]
