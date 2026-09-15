"""Tool-event hook factory + permission-evaluator owner.

Extracted from :mod:`ember_code.core.session.core` — the
``_create_tool_event_hook`` helper (and the inline
:class:`PermissionEvaluator` construction it did as a side effect)
graduates to a dedicated class here.

Owns the two invariants previously scattered across
``Session._create_tool_event_hook`` + a bare
``self.permission_evaluator = ...`` write:

* The evaluator is built from ``settings.permissions.*`` and
  cached — every ``create()`` call reuses the same instance so
  the mode flip performed by :class:`RuntimeModeCoordinator`
  survives hook rebuilds.
* The :class:`ToolEventHook` is composed with the cached
  evaluator plus the current :class:`HookExecutor` /
  ``session_id`` (both looked up lazily so the factory tolerates
  the executor being rebuilt by ``reload_hooks``).

Rule 6 (oop_offender #12): a factory class replaces the two
sprawled operations on the Session god-class.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.config.settings import Settings
from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.tool_hook import ToolEventHook
from ember_code.core.utils.rules_index import RulesIndex


class ToolEventHookFactory:
    """Factory + owner of the :class:`ToolEventHook` used by the
    main team's ``tool_hooks`` list.

    One instance is composed by :class:`Session` at construction
    time; subsequent ``reload_hooks`` calls reuse it so the
    :class:`PermissionEvaluator` cache persists across the
    hook-executor rebuild.
    """

    def __init__(
        self,
        settings: Settings,
        rules_index: RulesIndex,
        project_dir: Path,
        hook_executor_ref: Callable[[], HookExecutor],
        session_id_ref: Callable[[], str],
    ) -> None:
        self._settings = settings
        self._rules_index = rules_index
        self._project_dir = project_dir
        self._hook_executor_ref = hook_executor_ref
        self._session_id_ref = session_id_ref
        self._permission_evaluator: PermissionEvaluator | None = None

    @property
    def permission_evaluator(self) -> PermissionEvaluator:
        """Return the cached :class:`PermissionEvaluator`, building
        one on first access.

        Exposed as a property so :class:`Session` can forward its
        legacy ``session.permission_evaluator`` attribute without
        needing to know when the evaluator was constructed.
        """
        if self._permission_evaluator is None:
            perms = self._settings.permissions
            # The per-category levels (``file_write``, ``shell_execute``, …) are
            # appended behind the explicit lists. They are documented in
            # SECURITY.md as igni's permission model and were read by nothing:
            # their only consumer, ``PermissionGuard``, was never constructed.
            # Appended rather than merged, so an explicit rule is never
            # dropped. Note that ordering does NOT create an exception: the
            # evaluator runs deny before allow and treats deny as its safety
            # floor, so a category set to ``deny`` cannot be reopened by a
            # specific allow. Verified, not assumed — see
            # tests/test_permission_categories_are_live.py.
            cat_deny, cat_ask, cat_allow = perms.category_rules()
            self._permission_evaluator = PermissionEvaluator.from_strings(
                mode=perms.mode,
                deny=[*perms.deny, *cat_deny],
                ask=[*perms.ask, *cat_ask],
                allow=[*perms.allow, *cat_allow],
            )
        return self._permission_evaluator

    def create(self) -> ToolEventHook:
        """Build a fresh :class:`ToolEventHook` bound to the current
        hook executor / session id / cached evaluator.

        Called on every ``reload_hooks`` so the tool_hooks list
        picks up new event definitions; the cached evaluator is
        preserved intentionally (mode flips must survive reload).
        """
        return ToolEventHook(
            executor=self._hook_executor_ref(),
            session_id=self._session_id_ref(),
            protected_paths=self._settings.safety.protected_paths,
            blocked_commands=self._settings.safety.blocked_commands,
            rules_index=self._rules_index,
            project_dir=self._project_dir,
            permission_evaluator=self.permission_evaluator,
        )

    def evaluator_for_session(self, session: Any) -> PermissionEvaluator:
        """Compat wrapper for :class:`Session` — build the
        evaluator AND write it onto ``session.permission_evaluator``
        so legacy callers that reach for the attribute keep
        working. Returns the same instance :attr:`permission_evaluator`
        returns.
        """
        evaluator = self.permission_evaluator
        session.permission_evaluator = evaluator
        return evaluator
