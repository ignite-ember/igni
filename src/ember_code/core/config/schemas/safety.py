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


class SafetyConfig(BaseModel):
    protected_paths: list[str] = Field(default_factory=lambda: list(_DEFAULT_PROTECTED_PATHS))
    blocked_commands: list[str] = Field(default_factory=lambda: list(_DEFAULT_BLOCKED_COMMANDS))
    max_file_size_kb: int = 500
    require_confirmation: list[str] = Field(default_factory=lambda: list(_DEFAULT_CONFIRM_COMMANDS))
