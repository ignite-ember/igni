"""Permission types shared across the tool-approval path.

This package used to hold a second, parallel permission system —
``PermissionGuard`` orchestrating ``PermissionPolicy``, ``ApprovalPrompt``,
``AllowlistStore`` and ``SessionApprovalCache`` behind ``check_file_write`` /
``check_shell_execute``. It was never constructed. Not once, in ``src`` or in
``tests``: its own docstring named ``core/session/core.py`` as the caller and
that file did not mention it.

The cost was not the dead code. ``PermissionsConfig.file_write`` and
``shell_execute`` were documented in SECURITY.md, CONFIGURATION.md and
QUICKSTART.md as igni's permission model, and the schema said they were
"interpreted by the older ``PermissionGuard``" — so a config file saying
``shell_execute: "deny"`` did nothing, and ``--no-web`` denied no web. Twenty-one
passing tests covered the machinery, which is what made it look maintained.

Those levels are now honoured by the live evaluator, via
``PermissionsConfig.category_rules``. What remains here is the shared type
vocabulary — ``PermissionLevel`` and ``PermissionRequest`` are used by the HITL
controller and the hook events, and ``PermissionCategory`` by
``tool_permissions.schemas``.
"""

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

__all__ = [
    "AllowlistFile",
    "AllowlistPattern",
    "ApprovalChoice",
    "DecisionSource",
    "GuardDecision",
    "PermissionCategory",
    "PermissionLevel",
    "PermissionRequest",
]
