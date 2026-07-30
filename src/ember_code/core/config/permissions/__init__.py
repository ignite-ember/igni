"""Permission system — tool call approval with persistent allowlists.

Package facade. Re-exports the OOP collaborators so external callers
(``ember_code.core.session.core``, tests) continue to import from
``ember_code.core.config.permissions`` unchanged.

Module layout:
    * :mod:`schemas`         — Pydantic / StrEnum types
    * :mod:`allowlist_store` — YAML persistence
    * :mod:`session_cache`   — session-scope one-time approvals
    * :mod:`policy`          — pure decision pipeline
    * :mod:`prompt`          — interactive I/O
    * :mod:`guard`           — slim orchestrator (public API)
"""

from ember_code.core.config.permissions.allowlist_store import AllowlistStore
from ember_code.core.config.permissions.guard import PermissionGuard
from ember_code.core.config.permissions.policy import PermissionPolicy
from ember_code.core.config.permissions.prompt import ApprovalPrompt
from ember_code.core.config.permissions.schemas import (
    AllowlistFile,
    AllowlistPattern,
    ApprovalChoice,
    DecisionSource,
    GuardDecision,
    PermissionCategory,
    PermissionLevel,
    PermissionRequest,
)
from ember_code.core.config.permissions.session_cache import SessionApprovalCache

__all__ = [
    "AllowlistFile",
    "AllowlistPattern",
    "AllowlistStore",
    "ApprovalChoice",
    "ApprovalPrompt",
    "DecisionSource",
    "GuardDecision",
    "PermissionCategory",
    "PermissionGuard",
    "PermissionLevel",
    "PermissionPolicy",
    "PermissionRequest",
    "SessionApprovalCache",
]
