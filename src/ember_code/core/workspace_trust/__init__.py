"""Workspace trust: opening a folder never runs its code until the person trusts it.

* :mod:`.identity` — a folder's canonical path and git remote, the store's key.
* :mod:`.store` — the person's decisions, in ``~/.igni/workspace-trust.json``.
* :mod:`.service` — :class:`WorkspaceTrust`, which every gated loader asks.

Organisation policy is ``workspace_trust`` in settings
(:class:`~ember_code.core.config.schemas.workspace_trust.WorkspaceTrustConfig`),
honoured only from the managed file, the group policy pack and the environment.
"""

from ember_code.core.workspace_trust.identity import (
    WorkspaceIdentity,
    canonical_remote,
    remote_matches,
)
from ember_code.core.workspace_trust.service import (
    GATED_SUMMARY,
    TrustDecision,
    TrustRefused,
    WorkspaceTrust,
)
from ember_code.core.workspace_trust.store import TrustEntry, WorkspaceTrustStore

__all__ = [
    "GATED_SUMMARY",
    "TrustDecision",
    "TrustEntry",
    "TrustRefused",
    "WorkspaceIdentity",
    "WorkspaceTrust",
    "WorkspaceTrustStore",
    "canonical_remote",
    "remote_matches",
]
