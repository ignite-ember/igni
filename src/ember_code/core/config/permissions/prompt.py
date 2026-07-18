"""Interactive approval prompt.

Owns the console + Rich rendering + user input parsing that used to
live inside ``PermissionGuard._prompt_approval``. The console is
INJECTED at construction — the module-level ``Console()`` singleton
from the pre-refactor file is gone, so tests can inject a captured
console without monkey-patching.

Also owns the ``ApprovalChoice``-keyed dispatch dict, replacing the
``if choice == "y": ... elif choice == "a": ...`` chain from the
original with a polymorphic ``{choice: bound_method}`` lookup.
"""

from collections.abc import Callable

from rich.console import Console
from rich.prompt import Prompt

from ember_code.core.config.permissions.allowlist_store import AllowlistStore
from ember_code.core.config.permissions.schemas import (
    AllowlistPattern,
    ApprovalChoice,
    DecisionSource,
    DecisionVerdict,
    GuardDecision,
    PermissionRequest,
)
from ember_code.core.config.permissions.session_cache import SessionApprovalCache


class ApprovalPrompt:
    """Renders the four-option approval prompt and turns the user's
    choice into a :class:`GuardDecision`.

    Single responsibility: interactive I/O. Persistence writes go
    through ``self._store``; session-scope memoisation through
    ``self._cache``.
    """

    # Maps the single-key user input to the semantic ApprovalChoice.
    _KEY_TO_CHOICE: dict[str, ApprovalChoice] = {
        "y": ApprovalChoice.ONCE,
        "a": ApprovalChoice.ALWAYS,
        "s": ApprovalChoice.SIMILAR,
        "n": ApprovalChoice.DENY,
    }

    def __init__(
        self,
        console: Console,
        cache: SessionApprovalCache,
        store: AllowlistStore,
    ) -> None:
        self._console = console
        self._cache = cache
        self._store = store
        # Bound-method dispatch — each handler is a real method on
        # this instance and can freely touch ``self._cache`` /
        # ``self._store``.
        self._handlers: dict[ApprovalChoice, Callable[[PermissionRequest], GuardDecision]] = {
            ApprovalChoice.ONCE: self._handle_once,
            ApprovalChoice.ALWAYS: self._handle_always,
            ApprovalChoice.SIMILAR: self._handle_similar,
            ApprovalChoice.DENY: self._handle_deny,
        }

    # ── public API ────────────────────────────────────────────────

    def ask(self, request: PermissionRequest) -> GuardDecision:
        """Render the prompt and return a :class:`GuardDecision`.

        Short-circuits with a session-cache hit before rendering
        anything — matches the original ``_prompt_approval`` behaviour
        where a repeat call inside the same session skipped the
        prompt.
        """
        if self._cache.contains(request.category, request.value):
            return GuardDecision(
                allowed=True,
                verdict=DecisionVerdict.ALLOW,
                reason="session cached",
                source=DecisionSource.SESSION,
            )

        self._render(request)
        raw = Prompt.ask("  Choice", choices=list(self._KEY_TO_CHOICE.keys()), default="n")
        choice = self._KEY_TO_CHOICE[raw]
        return self._handlers[choice](request)

    # ── internal ──────────────────────────────────────────────────

    def _render(self, request: PermissionRequest) -> None:
        # Plain text with Rich markup — no emoji glyphs (Rule 3).
        self._console.print(f"\n[yellow]Permission required:[/yellow] {request.description}")
        self._console.print("  [y] Yes, allow once")
        self._console.print("  [a] Always allow")
        self._console.print("  [s] Allow similar")
        self._console.print("  [n] No, deny")

    def _handle_once(self, request: PermissionRequest) -> GuardDecision:
        self._cache.remember(request.category, request.value)
        return GuardDecision(
            allowed=True,
            verdict=DecisionVerdict.ALLOW,
            reason="user approved (once)",
            source=DecisionSource.USER,
        )

    def _handle_always(self, request: PermissionRequest) -> GuardDecision:
        self._store.add(request.category, AllowlistPattern(pattern=request.value))
        return GuardDecision(
            allowed=True,
            verdict=DecisionVerdict.ALLOW,
            reason="user approved (always)",
            source=DecisionSource.USER,
        )

    def _handle_similar(self, request: PermissionRequest) -> GuardDecision:
        pattern = AllowlistPattern.from_value(request.value)
        self._store.add(request.category, pattern)
        self._console.print(f"  [dim]Added pattern: {pattern.pattern}[/dim]")
        return GuardDecision(
            allowed=True,
            verdict=DecisionVerdict.ALLOW,
            reason=f"user approved (similar: {pattern.pattern})",
            source=DecisionSource.USER,
        )

    def _handle_deny(self, request: PermissionRequest) -> GuardDecision:
        return GuardDecision(
            allowed=False,
            verdict=DecisionVerdict.DENY,
            reason="user denied",
            source=DecisionSource.USER,
        )
