"""Pydantic schemas for the hooks system."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ``HookMatcher`` is a leaf module — its own dependencies (re,
# logging, typing) never reach back into schemas.py. The
# top-level import here inverts the pre-refactor cycle where
# :meth:`HookDefinition.matches` did the import lazily; do NOT
# reintroduce that inline import.
from ember_code.core.hooks.matcher import HookMatcher

if TYPE_CHECKING:
    from pathlib import Path

HookType = Literal["command", "http", "prompt", "mcp_tool", "agent"]


class PermissionDecision(str, Enum):
    """Wire-compatible permission decisions from a hook.

    Matches Claude Code's ``hookSpecificOutput.permissionDecision``:

    - ``ALLOW`` — skip the rest of the permission pipeline, run.
    - ``DENY`` — block the tool call and fire ``PermissionDenied``.
    - ``ASK`` — fire ``PermissionRequest``, treat as deny until the
      ``canUseTool`` bridge lands.
    - ``DEFER`` — no opinion, fall through to the rest of the pipeline.
    - ``NONE`` — the hook didn't set a decision (default).
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    DEFER = "defer"
    NONE = ""

    @classmethod
    def from_wire(cls, raw: str) -> PermissionDecision:
        """Parse a wire string; unknown values coerce to ``NONE``."""
        try:
            return cls(raw)
        except ValueError:
            return cls.NONE


class MergeStrategy(str, Enum):
    """How a caller wants freshly-parsed hooks merged into an existing
    :class:`HookRegistry` bucket.

    - ``APPEND`` — new hooks land at the end of the event's list.
      Used by settings-file loads so later files layer on top of
      earlier ones (project overrides user).
    - ``PREPEND`` — new hooks land at the front of the event's
      list. Used by plugin loads so plugin-supplied behavior fires
      *before* project hooks, letting the project's veto / transform
      still get the final word.
    """

    APPEND = "append"
    PREPEND = "prepend"


class HookDefinition(BaseModel):
    """A single hook definition.

    Five handler types modelled on Claude Code's catalog
    (``mcp_tool``, ``prompt``, and the ``agent`` type the spec
    also names):

    - ``command`` — shell command; the agent's payload goes in on
      stdin, exit codes drive control flow (2 blocks, 0 + JSON is
      the structured success path).
    - ``http`` — POST the payload to ``url`` with optional
      ``headers``; non-200 is non-blocking.
    - ``prompt`` — no side effect, just injects ``text`` back to
      the agent as a system reminder. Cheaper than the
      command-that-echoes-JSON pattern for nudge-style hooks.
    - ``mcp_tool`` — invokes the named MCP server tool with
      ``{event, payload, ...mcp_args}`` as input; the tool's
      stringified return becomes the hook's system message.
    """

    # Kept as ``str`` (not the ``HookType`` Literal) so unknown/misspelt
    # values validate successfully; the handler-registry dispatch then
    # gracefully skips them (see ``tests/test_hook_handler_types.py``).
    # No default: SettingsFile.save uses ``exclude_defaults=True`` and
    # the emitted settings.json must always carry an explicit ``type``.
    type: str
    command: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    # ``prompt`` handler: static text injected as the system
    # reminder when this hook fires.
    text: str = ""
    # ``mcp_tool`` handler: which MCP server + tool to call, plus
    # any static args merged into the call alongside
    # ``{event, payload}``.
    mcp_server: str = ""
    mcp_tool: str = ""
    mcp_args: dict[str, Any] = Field(default_factory=dict)
    matcher: str = ""
    timeout: int = 10000
    background: bool = False  # fire-and-forget, don't block the agent
    # ``asyncRewake``: hook runs in the background (like
    # ``background``), but if it exits with code 2 the combined
    # stderr+stdout is queued as a system reminder for the next
    # ``handle_message`` turn. Lets long-running hooks "wake" the
    # agent later with context. Settings.json may use either
    # ``asyncRewake`` (camelCase, CC-compatible) or
    # ``async_rewake`` — loader accepts both via
    # :meth:`from_wire`.
    async_rewake: bool = False

    def matches(self, target: str) -> bool:
        """Whether this hook fires for ``target``.

        Empty ``matcher`` matches every target (the pre-refactor
        default). Delegates to :class:`HookMatcher` for the actual
        pattern semantics (exact / regex / pipe-list).
        """
        return HookMatcher(self.matcher).matches(target)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HookDefinition:
        """Validate a raw settings-file dict into a :class:`HookDefinition`.

        Thin wrapper over :meth:`model_validate` — kept as an explicit
        classmethod so callers spell out the intent (parsing wire /
        settings input) at the seam.
        """
        return cls.model_validate(raw)

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> HookDefinition:
        """Validate a wire dict, normalising CC-compatible quirks first.

        Two normalisations happen here instead of in the loader:

        * ``asyncRewake`` (camelCase, CC-compatible) is aliased to
          ``async_rewake`` — Ember-native settings prefer the
          snake_case form, but a user copy-pasting from CC docs
          should not need to rewrite the key.
        * ``type`` defaults to ``"command"`` — matches the
          pre-refactor tolerance where a settings file omitting the
          type field implicitly got the command handler.

        The input dict is not mutated (a shallow copy is made) so
        callers who inspect the raw block after loading see it
        unchanged.
        """
        normalised = dict(raw)
        if "asyncRewake" in normalised and "async_rewake" not in normalised:
            normalised["async_rewake"] = normalised.pop("asyncRewake")
        normalised.setdefault("type", "command")
        return cls.model_validate(normalised)


class SafetyCheckResult(BaseModel):
    """Result of a synchronous safety-list check on a single tool call.

    Pattern 3 Result-style return for the pure predicate seam
    on :class:`~ember_code.core.hooks.permission_pipeline.ProtectedPathStage`
    and :class:`~ember_code.core.hooks.permission_pipeline.BlockedCommandStage`.
    Their async ``evaluate`` composes ``check_sync`` (pure) with
    logging + :class:`StageOutcome` translation on the outside.

    ``ok=True`` means "this stage does not block" — it covers BOTH
    the "check does not apply to this tool" case AND the
    "check applies and passes" case. It is emphatically NOT an
    affirmative greenlight; do not confuse it with
    :class:`PermissionDecision.ALLOW`, which asserts a positive
    approval that can skip downstream evaluator gates.

    ``ok=False`` requires a non-empty ``block_message`` — the
    invariant is enforced by :meth:`block`.
    """

    ok: bool
    block_message: str = ""

    @property
    def blocked(self) -> bool:
        """Inverse of :attr:`ok` — reads naturally at call sites."""
        return not self.ok

    @classmethod
    def no_block(cls) -> SafetyCheckResult:
        """Construct a passing / non-applicable result.

        Named ``no_block`` (not ``allow``) so callers don't confuse
        it with :class:`PermissionDecision.ALLOW`. The stage is
        saying "I have no reason to block" — not "I affirmatively
        approve this call."
        """
        return cls(ok=True, block_message="")

    @classmethod
    def block(cls, message: str) -> SafetyCheckResult:
        """Construct a blocking result with a user-facing ``message``.

        Empty ``message`` raises :class:`ValueError` — the pipeline
        would otherwise translate this into ``Block('')`` and
        surface a blank error to the model.
        """
        if not message:
            raise ValueError("SafetyCheckResult.block() requires a non-empty message")
        return cls(ok=False, block_message=message)


class HookResult(BaseModel):
    """Result from a hook execution.

    ``permission_decision`` is the CC-compatible structured
    envelope for ``PreToolUse``-event hooks. When set, it
    overrides the boolean ``should_continue`` for permission
    routing — the four values map onto the same
    ``PermissionDecision`` enum the evaluator uses:

    - ``"allow"`` → skip the rest of the permission pipeline
      and run the tool (the hook approved it).
    - ``"deny"`` → block the tool call, fire
      ``PermissionDenied``.
    - ``"ask"`` → fire ``PermissionRequest``, treat as deny
      until the ``canUseTool`` bridge lands.
    - ``"defer"`` (or empty) → no opinion, fall through to the
      rest of the pipeline (legacy + evaluator + tool call).

    Other event types continue to use ``should_continue`` only.
    """

    should_continue: bool = True
    message: str = ""
    permission_decision: str = ""


# Alias for downstream code that discriminates between
# ``HookDefinition`` (concrete flat model) and the "base" type
# used for type hints on collections. Both resolve to the same
# class today; the alias is retained for import compatibility.
HookDefinitionBase = HookDefinition


HookLoadWarningKind = Literal[
    "invalid_json",
    "os_error",
    "non_dict_hook",
    "validation_error",
    "non_dict_block",
]


class HookLoadWarning(BaseModel):
    """A structured warning emitted during hook loading.

    Replaces the pre-refactor ``print(..., file=sys.stderr)`` sites
    scattered across :class:`HookLoader`. Now that warnings are
    Pydantic models, callers can log, batch, or surface them in a
    UI (the backend hooks panel already snapshots ``hooks_map`` —
    a next iteration can attach ``warnings`` next to it).

    ``source`` is stored as a plain string (not a :class:`Path`) so
    the model round-trips cleanly through JSON without needing
    :class:`ConfigDict` gymnastics.
    """

    source: str
    kind: HookLoadWarningKind
    detail: str

    @classmethod
    def from_path(
        cls,
        path: Path,
        kind: HookLoadWarningKind,
        detail: str,
    ) -> HookLoadWarning:
        """Construct a warning from a :class:`Path` source."""
        return cls(source=str(path), kind=kind, detail=detail)


class HookLoadResult(BaseModel):
    """Outcome of a :meth:`HookLoader.load` (or plugin-hooks) call.

    Pairs the populated :class:`HookRegistry` with any structured
    warnings that surfaced during parsing. Callers unwrap
    ``.registry`` to attach the hooks to a session and log
    ``.warnings`` through whatever channel is appropriate.

    ``registry`` is typed as :class:`Any` (rather than
    :class:`HookRegistry`) so tests that swap the loader out with
    a :class:`unittest.mock.MagicMock` don't trip on Pydantic's
    strict isinstance check. Production callers still get the
    real class — the field's docstring here is the source of
    truth for the intended type.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ``registry`` is a :class:`HookRegistry` in practice; typed
    # as ``Any`` to accept MagicMock instances in the test suite
    # (Pydantic's isinstance validation is strict even under
    # ``arbitrary_types_allowed=True``).
    registry: Any
    warnings: list[HookLoadWarning] = Field(default_factory=list)

    def merge(self, other: HookLoadResult) -> HookLoadResult:
        """Fold *other* into this result and return a fresh instance.

        The two registries share the SAME underlying dict — plugin
        loads mutate the settings-load registry in place — so this
        method's job is really just to concatenate warning lists.
        The returned :class:`HookLoadResult` references the shared
        registry so downstream mutation on either side stays
        visible.
        """
        return HookLoadResult(
            registry=self.registry,
            warnings=[*self.warnings, *other.warnings],
        )


class HookPayloadBase(BaseModel):
    """Base for typed per-event payloads.

    ``PreToolUsePayload``, ``PostToolUsePayload``, etc. will subclass
    this once the per-event payload migration lands. Today it exists
    as a common isinstance root so :meth:`HookExecutor.execute` can
    accept both typed subclasses and the permissive
    :class:`GenericHookPayload` shim.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class GenericHookPayload(HookPayloadBase):
    """Permissive payload wrapper for callers that still pass raw dicts.

    ``extra="allow"`` accepts arbitrary caller-supplied fields so the
    executor can dump the payload back to a dict for wire dispatch
    without losing information.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class HookPayload(GenericHookPayload):
    """Legacy alias for :class:`GenericHookPayload`.

    Exposes :meth:`coerce` — the entry point every handler uses to
    accept either a raw dict, a :class:`HookPayloadBase` subclass,
    or an existing :class:`HookPayload`.
    """

    @classmethod
    def coerce(cls, raw: HookPayloadBase | dict[str, Any]) -> HookPayload:
        """Coerce a dict or typed payload into a :class:`HookPayload`.

        Typed payloads pass through as-is (their fields survive via
        ``extra="allow"``). Raw dicts are validated into a permissive
        payload so downstream handlers get a uniform shape.
        """
        if isinstance(raw, HookPayloadBase):
            return cls.model_validate(raw.model_dump())
        return cls.model_validate(raw)

    def to_wire_dict(self) -> dict[str, Any]:
        """Dump back to a plain dict for wire dispatch."""
        return self.model_dump(exclude_none=True)
