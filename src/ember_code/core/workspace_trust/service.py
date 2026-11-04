"""Whether a folder is trusted to run its own code, and the one place that says so.

Every loader that would run something a folder brings — its
``.igni/tools/*.py``, its and its ``.claude`` hooks, its workflows, its
``.lsp.json`` and ``.monitors.json`` commands, its plugins, a markdown
command's ``!`cmd``` tokens — and the settings merge, which takes a folder's
``permissions`` and ``safety`` only from a trusted one, asks
:meth:`WorkspaceTrust.is_trusted` first. Reading files, ``igni.md`` and rules
does not need trust.

The answer, in order:

1. **Organisation policy** (``workspace_trust``, managed-only): ``never`` →
   untrusted; ``trusted_remotes`` with a remote that matches no pattern →
   untrusted, and the person cannot override it. With ``auto_trust_remotes``
   a matching remote is trusted without asking.
2. **This run**: "This time only", or ``--trust`` on a headless run.
3. **The person's recorded decision** (:class:`WorkspaceTrustStore`).
4. **Unset** → ``ask``. Until someone answers, the folder runs untrusted.

One instance per process (:meth:`shared`), like the store it reads: the
settings loader hands it the organisation's policy each time settings load.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from ember_code.core.config.schemas.workspace_trust import WorkspaceTrustConfig
from ember_code.core.workspace_trust.identity import WorkspaceIdentity, remote_matches
from ember_code.core.workspace_trust.store import TrustEntry, WorkspaceTrustStore

logger = logging.getLogger(__name__)

DecisionState = Literal["trusted", "untrusted", "ask"]
DecisionSource = Literal["policy", "run", "store", "unset", "test"]

#: What an untrusted folder does not get, for prompts and logs.
GATED_SUMMARY = (
    "project tools, hooks, workflows, language servers, monitors, plugins, "
    "markdown-command shell tokens, and its permission and safety settings"
)


class TrustDecision(BaseModel):
    """The answer for one folder, with enough to explain it."""

    identity: WorkspaceIdentity
    state: DecisionState
    source: DecisionSource
    #: Whether the person may trust this folder at all (policy permits it).
    can_trust: bool = True
    #: Human-readable reason, shown in prompts and the ``igni trust`` output.
    reason: str = ""
    policy_mode: str = "ask"

    @property
    def trusted(self) -> bool:
        return self.state == "trusted"

    @property
    def needs_answer(self) -> bool:
        return self.state == "ask"


class TrustRefused(Exception):
    """The organisation's policy does not let this folder be trusted."""


class WorkspaceTrust:
    """Decides, records and answers workspace trust for this process."""

    _shared: ClassVar[WorkspaceTrust | None] = None
    _shared_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        store: WorkspaceTrustStore | None = None,
        policy: WorkspaceTrustConfig | None = None,
        *,
        trust_everything: bool = False,
    ) -> None:
        self.store = store or WorkspaceTrustStore()
        self._policy = policy
        #: Identity keys trusted for this process only ("This time only", ``--trust``).
        self._run_grants: set[str] = set()
        #: Identity keys the person answered "Don't trust" for in this process
        #: without it being recorded (used by clients that ask per run).
        self._listeners: list[Callable[[TrustDecision], None]] = []
        self._trust_everything = trust_everything
        self._logged: set[tuple[str, str]] = set()

    # ── process-wide instance ─────────────────────────────────────

    @classmethod
    def shared(cls) -> WorkspaceTrust:
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls()
            return cls._shared

    @classmethod
    def reset_shared(cls, *, trust_everything: bool = False) -> WorkspaceTrust:
        """A fresh process-wide instance. ``trust_everything`` is a test seam:
        the suite's older tests are about other things and open project
        folders as a trusted one would be."""
        with cls._shared_lock:
            cls._shared = cls(trust_everything=trust_everything)
            return cls._shared

    # ── policy ────────────────────────────────────────────────────

    @property
    def policy(self) -> WorkspaceTrustConfig:
        if self._policy is None:
            # No settings loaded yet in this process: the environment still
            # applies (the config's validator reads it).
            self._policy = WorkspaceTrustConfig()
        return self._policy

    def set_policy(self, policy: WorkspaceTrustConfig) -> None:
        self._policy = policy

    # ── deciding ──────────────────────────────────────────────────

    def decide(self, project_dir: str | Path) -> TrustDecision:
        identity = WorkspaceIdentity.of(project_dir)
        policy = self.policy
        mode = policy.mode

        if self._trust_everything:
            return TrustDecision(
                identity=identity, state="trusted", source="test", policy_mode=mode
            )

        if mode == "never":
            return TrustDecision(
                identity=identity,
                state="untrusted",
                source="policy",
                can_trust=False,
                reason="Your organisation's policy (workspace_trust.mode: never) "
                "does not allow any folder to be trusted.",
                policy_mode=mode,
            )
        if mode == "trusted_remotes":
            if not remote_matches(identity.remote, policy.trusted_remotes):
                why = (
                    "this folder has no git remote"
                    if not identity.remote
                    else f"its git remote ({identity.remote}) is not one of them"
                )
                return TrustDecision(
                    identity=identity,
                    state="untrusted",
                    source="policy",
                    can_trust=False,
                    reason="Your organisation only allows folders from approved git "
                    f"remotes to be trusted, and {why}.",
                    policy_mode=mode,
                )
            if policy.auto_trust_remotes:
                return TrustDecision(
                    identity=identity,
                    state="trusted",
                    source="policy",
                    reason="Trusted by your organisation's policy: its git remote is approved.",
                    policy_mode=mode,
                )

        if identity.key in self._run_grants:
            return TrustDecision(
                identity=identity,
                state="trusted",
                source="run",
                reason="Trusted for this run.",
                policy_mode=mode,
            )

        recorded = self.store.get(identity)
        if recorded is not None:
            return TrustDecision(
                identity=identity,
                state=recorded,
                source="store",
                reason=f"You chose to {'trust' if recorded == 'trusted' else 'not trust'} "
                "this folder.",
                policy_mode=mode,
            )
        return TrustDecision(
            identity=identity,
            state="ask",
            source="unset",
            reason="You have not decided whether to trust this folder yet.",
            policy_mode=mode,
        )

    def is_trusted(self, project_dir: str | Path, *, what: str = "") -> bool:
        """True when ``project_dir`` may run its own code. ``what`` names the
        caller for a once-per-folder log line when the answer is no."""
        decision = self.decide(project_dir)
        if not decision.trusted and what:
            mark = (decision.identity.key, what)
            if mark not in self._logged:
                self._logged.add(mark)
                logger.info(
                    "Workspace %s is not trusted; skipping its %s. %s",
                    decision.identity.path,
                    what,
                    decision.reason,
                )
        return decision.trusted

    # ── answering ─────────────────────────────────────────────────

    def trust(self, project_dir: str | Path, *, remember: bool = True) -> TrustDecision:
        """Trust ``project_dir`` — for good (``remember``) or for this run.

        Raises :class:`TrustRefused` when the organisation's policy does not
        allow this folder to be trusted.
        """
        decision = self.decide(project_dir)
        if not decision.can_trust:
            raise TrustRefused(decision.reason)
        if remember:
            self.store.set(decision.identity, "trusted")
        else:
            self._run_grants.add(decision.identity.key)
        return self._changed(project_dir)

    def distrust(self, project_dir: str | Path, *, remember: bool = True) -> TrustDecision:
        """Record "Don't trust". Also ends a this-run grant."""
        identity = WorkspaceIdentity.of(project_dir)
        self._run_grants.discard(identity.key)
        if remember:
            self.store.set(identity, "untrusted")
        return self._changed(project_dir)

    def revoke(self, project_dir: str | Path) -> TrustDecision:
        """Forget every decision about ``project_dir``: it asks again."""
        identity = WorkspaceIdentity.of(project_dir)
        self._run_grants.discard(identity.key)
        self.store.forget(identity)
        return self._changed(project_dir)

    def grant_for_run(self, project_dir: str | Path) -> TrustDecision:
        """``--trust``: trusted for this process if policy allows it.

        Unlike :meth:`trust`, never raises: a headless run passing
        ``--trust`` where policy forbids it stays untrusted and says so.
        """
        decision = self.decide(project_dir)
        if not decision.can_trust:
            logger.warning("--trust ignored for %s: %s", decision.identity.path, decision.reason)
            return decision
        self._run_grants.add(decision.identity.key)
        return self.decide(project_dir)

    def entries(self) -> list[TrustEntry]:
        return self.store.entries()

    # ── change notification ───────────────────────────────────────

    def subscribe(self, listener: Callable[[TrustDecision], None]) -> Callable[[], None]:
        """Call ``listener`` after a decision changes in this process."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _changed(self, project_dir: str | Path) -> TrustDecision:
        decision = self.decide(project_dir)
        self._logged = {m for m in self._logged if m[0] != decision.identity.key}
        for listener in list(self._listeners):
            try:
                listener(decision)
            except Exception:  # noqa: BLE001 — a listener must not undo the decision
                logger.exception("workspace trust listener failed")
        return decision


__all__ = [
    "GATED_SUMMARY",
    "DecisionState",
    "TrustDecision",
    "TrustRefused",
    "WorkspaceTrust",
]
