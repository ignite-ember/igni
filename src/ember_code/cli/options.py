"""Typed bundle of Click callback parameters.

Kills the AP5 "17 loose booleans" callback signature by exposing
one strongly-typed :class:`CliOptions` object to the invocation
orchestrator. The Click decorator surface in
:mod:`ember_code.cli.__init__` builds this from ``ctx.params``
after Click parses the argv; the rest of the CLI reads only the
typed bundle.

The Pydantic model validates types once at the boundary — if
Click ever hands back a string for a flag that should be a bool
(a real bug we've hit before), the model construction fails
loudly instead of silently coercing at every consumer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CliOptions(BaseModel):
    """Strongly-typed view of the 17 Click callback parameters.

    Attribute names mirror the Click parameter names one-for-one so
    ``CliOptions.model_validate(ctx.params)`` just works. The
    ``add_dir`` field lands as a tuple (Click ``multiple=True``);
    the invocation resolves it to ``list[Path]`` when it needs the
    resolved directories.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # ── Model + display ─────────────────────────────────────────
    model: str | None = None
    verbose: bool = False
    quiet: bool = False

    # ── Message + resume ────────────────────────────────────────
    message: str | None = None
    continue_session: bool = False
    session_id: str | None = None

    # ── Permission-shaping flags ────────────────────────────────
    read_only: bool = False
    accept_edits: bool = False
    auto_approve: bool = False
    no_web: bool = False
    strict: bool = False

    # ── Modes + display ─────────────────────────────────────────
    pipe: bool = False
    no_color: bool = False
    debug: bool = False

    # ── Environment ─────────────────────────────────────────────
    worktree: bool = False
    add_dir: tuple[str, ...] = Field(default_factory=tuple)
