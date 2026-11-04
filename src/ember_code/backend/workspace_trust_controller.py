"""Workspace trust for the desktop and web: the backend's half of the dialog.

There is no terminal to prompt on, so the window asks. It reads the folder's
decision with ``get_workspace_trust`` when it connects or opens a session,
shows "Trust this folder?" while the state is ``ask``, and answers with
``set_workspace_trust``. Every answer is pushed on ``workspace_trust_required``
so another window showing the dialog for the same folder closes it.

Until someone answers, the folder runs untrusted — the loaders ask
:class:`~ember_code.core.workspace_trust.WorkspaceTrust` themselves. After an
answer the running session is rebuilt: settings reload (with or without the
folder's ``permissions`` / ``safety``), then plugins, hooks, pools, language
servers, monitors and the main team (:meth:`Session.apply_workspace_trust`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ember_code.core.config.settings import Settings, load_settings
from ember_code.core.workspace_trust import (
    GATED_SUMMARY,
    TrustDecision,
    WorkspaceTrust,
)

logger = logging.getLogger(__name__)

TrustAnswer = Literal["trust", "distrust", "once"]


class WorkspaceTrustStatus(BaseModel):
    """What ``get_workspace_trust`` returns and ``workspace_trust_required`` carries."""

    #: ``trusted`` / ``untrusted`` / ``ask`` (nobody has decided; runs untrusted).
    state: Literal["trusted", "untrusted", "ask"]
    #: False when the organisation's policy forbids trusting this folder.
    can_trust: bool
    reason: str
    #: Canonical folder path.
    path: str
    #: The folder's git remote (canonical, credentials removed), or ``None``.
    remote: str | None = None
    #: ``ask`` / ``trusted_remotes`` / ``never``.
    policy_mode: str
    #: What an untrusted folder does not get, for the dialog.
    gated: str = GATED_SUMMARY
    #: How the answer was reached: ``policy`` / ``run`` / ``store`` / ``unset``.
    source: str = ""

    @classmethod
    def of(cls, decision: TrustDecision) -> WorkspaceTrustStatus:
        return cls(
            state=decision.state,
            can_trust=decision.can_trust,
            reason=decision.reason,
            path=decision.identity.path,
            remote=decision.identity.remote,
            policy_mode=decision.policy_mode,
            source=decision.source,
        )


class WorkspaceTrustRefusedError(Exception):
    """An answer the backend will not record (policy, or an unknown answer)."""


def reload_settings(project_dir: Path) -> Settings:
    """Settings for ``project_dir`` the way the backend boots them: a local
    pass to find the group-policy cache, then the load that counts."""
    from ember_code.core.config.group_policy import (
        CachedSettingsFetcher,
        GroupPolicyUnavailable,
    )

    local_only = load_settings(project_dir=project_dir)
    try:
        return load_settings(
            project_dir=project_dir,
            group_policy_fetcher=CachedSettingsFetcher.for_settings(local_only),
        )
    except GroupPolicyUnavailable as exc:
        logger.error("group-policy: %s", exc)
        return local_only


class WorkspaceTrustController:
    """Status and answers for the folder one backend runtime serves."""

    def __init__(self, project_dir: Path, trust: WorkspaceTrust | None = None) -> None:
        self._project_dir = Path(project_dir)
        self._trust = trust

    @property
    def _service(self) -> WorkspaceTrust:
        # Read per call: the process-wide instance can be replaced (tests).
        return self._trust or WorkspaceTrust.shared()

    def status(self) -> WorkspaceTrustStatus:
        return WorkspaceTrustStatus.of(self._service.decide(self._project_dir))

    def answer(self, decision: str) -> tuple[WorkspaceTrustStatus, bool]:
        """Record ``decision``; returns the new status and whether trust changed.

        Raises :class:`WorkspaceTrustRefusedError` when the organisation's
        policy does not allow the folder to be trusted, or the answer is not
        one of ``trust`` / ``distrust`` / ``once``.
        """
        from ember_code.core.workspace_trust import TrustRefused

        before = self._service.decide(self._project_dir).trusted
        try:
            if decision == "trust":
                after = self._service.trust(self._project_dir, remember=True)
            elif decision == "once":
                after = self._service.trust(self._project_dir, remember=False)
            elif decision == "distrust":
                after = self._service.distrust(self._project_dir)
            else:
                raise WorkspaceTrustRefusedError(
                    f"Unknown answer {decision!r}: expected trust, distrust or once."
                )
        except TrustRefused as exc:
            raise WorkspaceTrustRefusedError(str(exc)) from exc
        return WorkspaceTrustStatus.of(after), after.trusted != before


__all__ = [
    "TrustAnswer",
    "WorkspaceTrustController",
    "WorkspaceTrustRefusedError",
    "WorkspaceTrustStatus",
    "reload_settings",
]
