"""Pydantic schemas and StrEnum types for the permission subsystem.

Type-safe replacements for the raw ``dict[str, list[str]]`` /
``str`` / ``bool`` shapes the legacy ``permissions.py`` used
throughout. Each class here is a data container — behaviour lives on
``AllowlistStore``, ``PermissionPolicy``, ``ApprovalPrompt``, and
``PermissionGuard`` in sibling modules.

Naming note: ``GuardDecision`` — NOT ``PermissionDecision`` — is the
verdict record returned by ``PermissionPolicy.evaluate`` and
``PermissionGuard.decide``. The name is deliberate: sibling module
``permission_eval`` already exports a ``PermissionDecision`` StrEnum
(ALLOW / DENY / ASK / DEFER) that is a different concept (a
pipeline-step verdict without a reason string). Sharing the name
would create an import clash for downstream callers that import both.
"""

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class PermissionCategory(str, Enum):
    """Permission categories mirroring ``PermissionsConfig`` field names.

    Values MUST match the field names on
    ``ember_code.core.config.settings.PermissionsConfig`` verbatim so
    ``getattr(settings.permissions, category.value)`` returns the
    per-category level string without a translation layer.
    """

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL_EXECUTE = "shell_execute"


class PermissionLevel(str, Enum):
    """Legacy per-category level values stored on ``PermissionsConfig``."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class DecisionSource(str, Enum):
    """Where a ``GuardDecision`` verdict originated from — useful for
    audit logs and debugging why a call was allowed or blocked."""

    POLICY = "policy"  # settled by settings.permissions.<cat> level
    ALLOWLIST = "allowlist"  # matched a saved allowlist pattern
    SESSION = "session"  # matched a session-scope one-time approval
    USER = "user"  # user answered the interactive prompt
    PROTECTED = "protected"  # blocked because the path is protected
    BLOCKED = "blocked"  # blocked because command matches blocklist


class ApprovalChoice(str, Enum):
    """User choice in the interactive approval prompt."""

    ONCE = "once"
    ALWAYS = "always"
    SIMILAR = "similar"
    DENY = "deny"


class AllowlistPattern(BaseModel):
    """A single allowlist glob pattern (file path or shell prefix).

    Owns the glob-heuristic previously encoded as a free
    ``@staticmethod`` on ``PermissionGuard._generate_pattern``.
    """

    pattern: str

    @classmethod
    def from_value(cls, value: str) -> "AllowlistPattern":
        """Derive a broadening glob pattern from a concrete value.

        Examples:
            "npm test"        -> "npm *"
            "pytest tests/"   -> "pytest *"
            "src/auth.py"     -> "src/*"
            "standalone"      -> "standalone"
        """
        parts = value.split()
        if len(parts) > 1:
            return cls(pattern=f"{parts[0]} *")
        path = Path(value)
        if path.parent != Path("."):
            return cls(pattern=f"{path.parent}/*")
        return cls(pattern=value)


class AllowlistFile(BaseModel):
    """On-disk shape of ``~/.ember/permissions.yaml``.

    Written by ``AllowlistStore``. Legacy files with the shape
    ``{allowlist: {file_write: ["src/*"]}}`` (raw strings, ``allowlist``
    key) are silently migrated on load — see ``AllowlistStore._load``.
    """

    entries: dict[PermissionCategory, list[AllowlistPattern]] = Field(default_factory=dict)


class PermissionRequest(BaseModel):
    """A single permission check request handed to the policy /
    prompt pipeline."""

    category: PermissionCategory
    value: str
    description: str


class DecisionVerdict(str, Enum):
    """Semantic outcome of a policy evaluation.

    Replaces the ``reason == 'defer_to_prompt'`` stringly-typed
    sentinel that used to signal "defer to interactive approval".
    ``ALLOW`` / ``DENY`` are terminal; ``DEFER`` means the guard
    should route the request to the interactive prompt.
    """

    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"


class GuardDecision(BaseModel):
    """Reasoned verdict returned by ``PermissionPolicy.evaluate`` and
    ``PermissionGuard.decide``.

    NOT the same type as ``permission_eval.PermissionDecision`` — see
    module docstring for the naming rationale.

    ``verdict`` is the semantic outcome (allow / deny / defer). The
    boolean ``allowed`` is kept as a wire-compatible shim for the
    three ``check_file_read`` / ``check_file_write`` /
    ``check_shell_execute`` adapters that still return ``bool``.
    ``verdict == DecisionVerdict.DEFER`` implies ``allowed=False``.
    """

    allowed: bool
    reason: str
    source: DecisionSource
    verdict: DecisionVerdict
