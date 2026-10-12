"""``SafetyConfig`` — the ``safety`` block of ``Settings``.

Extracted from :mod:`ember_code.core.config.settings`. Also relocates
the three long default-factory lists to module-level constants so the
policy content is grep-able and importable by tests that need to
assert coverage without spinning up a full :class:`SafetyConfig`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

_DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "credentials.*",
    "secrets.*",
)

_DEFAULT_BLOCKED_COMMANDS: tuple[str, ...] = (
    "rm -rf /",
    ":(){ :|:& };:",
)

_DEFAULT_CONFIRM_COMMANDS: tuple[str, ...] = (
    "git push",
    "git push --force",
    "npm publish",
    "pip install",
    "docker run",
    "terraform apply",
    "kubectl apply",
    "kubectl delete",
)


class BypassGuardConfig(BaseModel):
    """``safety.bypass_guard``: Bypass mode's last check — see ``core/hooks/bypass_guard.py``."""

    enabled: bool = True
    #: Laya probability at which a call is put to the person.
    threshold: float = 0.75
    #: Off: rules only, no model loaded.
    use_model: bool = True
    #: Ask the session's model what a stopped call would do, and show that
    #: in the dialog once it answers. Off: the stock reason only.
    explain: bool = True
    #: Seconds to wait for that answer before keeping the stock reason.
    explain_timeout: float = 30.0


class SafetyConfig(BaseModel):
    protected_paths: list[str] = Field(default_factory=lambda: list(_DEFAULT_PROTECTED_PATHS))
    blocked_commands: list[str] = Field(default_factory=lambda: list(_DEFAULT_BLOCKED_COMMANDS))
    max_file_size_kb: int = 500
    require_confirmation: list[str] = Field(default_factory=lambda: list(_DEFAULT_CONFIRM_COMMANDS))
    #: Bypass mode's last check — see ``core/hooks/bypass_guard.py``.
    bypass_guard: BypassGuardConfig = Field(default_factory=BypassGuardConfig)
