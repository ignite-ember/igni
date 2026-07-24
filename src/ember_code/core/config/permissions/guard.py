"""Slim orchestrator wiring :class:`PermissionPolicy` and
:class:`ApprovalPrompt` together behind the ``check_file_read`` /
``check_file_write`` / ``check_shell_execute`` façade the rest of the
codebase (``core/session/core.py``) still calls.
"""

from pathlib import Path

from rich.console import Console

from ember_code.core.config.permissions.allowlist_store import AllowlistStore
from ember_code.core.config.permissions.policy import PermissionPolicy
from ember_code.core.config.permissions.prompt import ApprovalPrompt
from ember_code.core.config.permissions.schemas import (
    DecisionVerdict,
    GuardDecision,
    PermissionCategory,
    PermissionRequest,
)
from ember_code.core.config.permissions.session_cache import SessionApprovalCache
from ember_code.core.config.settings import Settings


class PermissionGuard:
    """Two-field orchestrator (``_policy`` + ``_prompt``).

    Public entry points:
        * :meth:`check_file_read` / :meth:`check_file_write` /
          :meth:`check_shell_execute` — one-line adapters returning
          ``bool`` for back-compat with existing call sites.
        * :meth:`decide` — richer :class:`GuardDecision` return for
          callers that want the reason string / source tag.

    Construction remains positional-single-arg for ``session/core.py``:
    ``PermissionGuard(settings)``. Optional keyword args
    (``permissions_path``, ``console``, ``prompt``) exist for tests —
    ``prompt`` is the primary injection seam so tests can substitute
    a headless :class:`ApprovalPrompt` without patching class
    internals.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        permissions_path: Path | None = None,
        console: Console | None = None,
        prompt: ApprovalPrompt | None = None,
    ) -> None:
        path = permissions_path or (Path.home() / ".ember" / "permissions.yaml")
        store = AllowlistStore(path)
        self._policy = PermissionPolicy(settings, store)
        self._prompt = prompt or ApprovalPrompt(
            console or Console(),
            SessionApprovalCache(),
            store,
        )

    # ── back-compat surface ───────────────────────────────────────

    @property
    def permissions_path(self) -> Path:
        """The on-disk allowlist path. Preserved so external tooling
        that inspected ``guard.permissions_path`` on the pre-refactor
        class still resolves."""
        return self._policy.allowlist.path

    # ── public API ────────────────────────────────────────────────

    def check_file_read(self, path: str) -> bool:
        return self.decide(
            PermissionRequest(
                category=PermissionCategory.FILE_READ,
                value=path,
                description=f"Read file: {path}",
            )
        ).allowed

    def check_file_write(self, path: str) -> bool:
        return self.decide(
            PermissionRequest(
                category=PermissionCategory.FILE_WRITE,
                value=path,
                description=f"Write file: {path}",
            )
        ).allowed

    def check_shell_execute(self, command: str) -> bool:
        return self.decide(
            PermissionRequest(
                category=PermissionCategory.SHELL_EXECUTE,
                value=command,
                description=f"Run: {command}",
            )
        ).allowed

    def decide(self, request: PermissionRequest) -> GuardDecision:
        """Run the full pipeline: policy → (if verdict is DEFER) prompt."""
        decision = self._policy.evaluate(request)
        if decision.verdict is DecisionVerdict.DEFER:
            return self._prompt.ask(request)
        return decision
