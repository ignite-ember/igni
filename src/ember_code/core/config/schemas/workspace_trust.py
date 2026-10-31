"""``workspace_trust`` — the organisation's say over which folders igni trusts.

Opening a folder in igni must never run its code or loosen igni's safety
until the person trusts the folder. A folder's ``.igni/tools/*.py``, its
hooks, workflows, ``.lsp.json`` / ``.monitors.json`` commands, project
plugins and markdown commands' ``!`cmd``` tokens all run code, and its
config could empty the safety lists — so all of that waits for trust
(``core/workspace_trust``).

This block lets an organisation narrow that decision:

* ``ask`` (default) — the person decides, once per folder;
* ``trusted_remotes`` — only a folder whose git remote matches one of
  ``trusted_remotes`` may be trusted; any other stays untrusted and the
  prompt says why;
* ``never`` — every folder is untrusted.

**Managed-only.** Honoured from the managed-settings file, the group policy
pack and the environment, never from a user's or a project's config or the
command line: ``WorkspaceTrustTier`` in ``merge_plan.py`` puts the block back
to its defaults before the organisation tiers merge, so a repository cannot
trust itself and a person cannot opt out of their organisation's rule.

The environment (``IGNI_WORKSPACE_TRUST_MODE``,
``IGNI_WORKSPACE_TRUSTED_REMOTES``) may only *tighten*: its mode applies when
it is stricter than the configured one, and its remotes apply when no tier
named any. A shell variable is something anyone at the keyboard can set, so
it may not loosen what the organisation decided.
"""

from __future__ import annotations

import os
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

WorkspaceTrustMode = Literal["ask", "trusted_remotes", "never"]

#: Strictness, loosest first. The environment may move the mode up this list only.
_STRICTNESS: dict[str, int] = {"ask": 0, "trusted_remotes": 1, "never": 2}


class WorkspaceTrustConfig(BaseModel):
    """The organisation's workspace-trust policy (see the module docstring)."""

    MODE_ENV: ClassVar[str] = "IGNI_WORKSPACE_TRUST_MODE"
    REMOTES_ENV: ClassVar[str] = "IGNI_WORKSPACE_TRUSTED_REMOTES"

    #: ``ask`` / ``trusted_remotes`` / ``never``.
    mode: WorkspaceTrustMode = "ask"

    #: Git remote patterns a folder may be trusted for, in ``trusted_remotes``
    #: mode. Shell-style wildcards, matched against the remote with scheme,
    #: user and a trailing ``.git`` removed — so ``github.com/acme/*`` matches
    #: ``git@github.com:acme/app.git`` and ``https://github.com/acme/app``
    #: alike. A pattern written as a URL is normalised the same way.
    trusted_remotes: list[str] = Field(default_factory=list)

    #: In ``trusted_remotes`` mode, trust a folder whose remote matches without
    #: asking — for CI and other runs with nobody to ask. Off by default: a
    #: matching remote may be trusted, the person still decides.
    auto_trust_remotes: bool = False

    @model_validator(mode="after")
    def _from_environment(self) -> WorkspaceTrustConfig:
        raw_mode = os.environ.get(self.MODE_ENV, "").strip().lower()
        if raw_mode in _STRICTNESS and _STRICTNESS[raw_mode] > _STRICTNESS[self.mode]:
            self.mode = raw_mode  # type: ignore[assignment]  # narrowed by the membership check
        if not self.trusted_remotes:
            raw = os.environ.get(self.REMOTES_ENV, "")
            self.trusted_remotes = [p.strip() for p in raw.split(",") if p.strip()]
        return self


__all__ = ["WorkspaceTrustConfig", "WorkspaceTrustMode"]
