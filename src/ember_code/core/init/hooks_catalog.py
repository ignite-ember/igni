"""Pure-data catalog of built-in hooks shipped with the package.

Kept as its own module so future built-in hooks are added by
appending a :class:`BuiltInHookSpec` to :data:`BUILT_IN_HOOKS`
without touching orchestrator code — mirrors how
``core/output_styles/`` and ``bundled_agents/`` are catalog-shaped.

No classes, no logic — just a ``tuple`` literal, so no OOP
violation. The provisioning behaviour lives on
:class:`BuiltInHookSpec` itself (:meth:`write_script` and
:meth:`register_in`); the coordinator that walks the catalog is
:class:`ember_code.core.init.hook_provisioner.HookProvisioner`.
"""

from __future__ import annotations

from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.init.schemas import BuiltInHookSpec
from ember_code.core.init_templates import (
    POST_COMMIT_TODO_HOOK,
    PRE_PR_REVIEW_HOOK,
)

BUILT_IN_HOOKS: tuple[BuiltInHookSpec, ...] = (
    BuiltInHookSpec(
        filename="pre-pr-review.sh",
        content=PRE_PR_REVIEW_HOOK,
        event="PreToolUse",
        definition=HookDefinition(
            type="command",
            command=".ember/hooks/pre-pr-review.sh",
            matcher="Bash",
            timeout=15000,
        ),
    ),
    BuiltInHookSpec(
        filename="post-commit-todo.sh",
        content=POST_COMMIT_TODO_HOOK,
        event="PostToolUse",
        definition=HookDefinition(
            type="command",
            command=".ember/hooks/post-commit-todo.sh",
            matcher="Bash",
            timeout=15000,
            background=True,
        ),
    ),
)
