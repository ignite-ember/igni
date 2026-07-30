"""Typed config for :class:`~ember_code.core.tools.shell.EmberShellTools`.

Replaces the free-form ``**kwargs`` on the old ``__init__`` (AP5 —
untyped kwargs shape). Fields cover every option the two in-repo
call sites (``registry.py::_make_bash`` /
``backend/server_lifecycle.py``) actually pass; anything more exotic
should be added here explicitly rather than smuggled through kwargs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ShellToolsConfig(BaseModel):
    """Construction options for :class:`EmberShellTools`.

    Frozen so ``EmberShellTools`` can safely stash it and rely on
    the base_dir / confirm list not shifting under it after init.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Working directory for spawned commands. ``None`` uses the
    # current process cwd — matches ``asyncio.create_subprocess_shell``
    # default.
    base_dir: Path | None = None

    # Tool names Agno should route through HITL confirmation before
    # firing. ``None`` means no confirmation (the tool runs
    # unattended). See the ``registry.py::_make_bash`` factory.
    requires_confirmation_tools: list[str] | None = None

    # Toolkit name registered with Agno. Kept configurable so
    # sub-toolkits / plugin variants don't clash.
    name: str = "ember_shell"
