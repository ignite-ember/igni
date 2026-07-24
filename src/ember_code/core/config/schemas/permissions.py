"""``PermissionsConfig`` — the ``permissions`` block of ``Settings``.

Extracted from :mod:`ember_code.core.config.settings`. Pure data
schema — no methods.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PermissionsConfig(BaseModel):
    # Legacy per-category levels — interpreted by the older
    # ``PermissionGuard``. Kept untouched for back-compat; the new
    # ``PermissionEvaluator`` reads ``mode`` / ``deny`` / ``ask`` /
    # ``allow`` instead.
    file_read: str = "allow"
    file_write: str = "ask"
    shell_execute: str = "ask"
    shell_restricted: str = "allow"
    web_search: str = "allow"
    web_fetch: str = "allow"
    git_push: str = "ask"
    git_destructive: str = "ask"
    # Claude Code-style permission system (mirrors
    # ``settings.json``'s ``permissions`` block). ``mode`` is one
    # of ``default`` / ``dontAsk`` / ``acceptEdits`` /
    # ``bypassPermissions`` / ``plan``. ``deny`` / ``ask`` /
    # ``allow`` are lists of ``Tool`` or ``Tool(pattern)`` strings
    # (e.g. ``"Bash(rm *)"``, ``"Read(./.env)"``).
    mode: str = "default"
    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)
