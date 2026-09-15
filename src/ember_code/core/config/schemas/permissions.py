"""``PermissionsConfig`` — the ``permissions`` block of ``Settings``.

Extracted from :mod:`ember_code.core.config.settings`. Pure data
schema — no methods.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field


class PermissionsConfig(BaseModel):
    # Per-category levels. This comment used to say they were "interpreted by
    # the older ``PermissionGuard``" — they were not, because nothing ever
    # constructed one. They are honoured now via :meth:`category_rules`, which
    # turns any level differing from its default into rules for the live
    # ``PermissionEvaluator``.
    #
    # ``file_read`` and ``shell_restricted`` remain unread, and deliberately so:
    # see the note on ``CATEGORY_FUNCTIONS``.
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

    #: Which LLM function names each per-category level governs. Derived from
    #: the registry names rather than hand-listed so a renamed function does not
    #: quietly drop out of its category — the failure this whole mapping exists
    #: to end.
    #:
    #: ``file_read`` and ``shell_restricted`` are deliberately absent. igni has
    #: no read tool (a file is read through ``run_shell_command``), so a
    #: ``file_read`` rule would either do nothing or, if pointed at the shell,
    #: silently govern writes too.
    CATEGORY_FUNCTIONS: ClassVar[dict[str, tuple[str, ...]]] = {
        "file_write": (
            "save_file",
            "create_file",
            "edit_file",
            "edit_file_replace_all",
            "notebook_add_cell",
            "notebook_edit_cell",
            "notebook_remove_cell",
        ),
        "shell_execute": ("run_shell_command",),
        "web_search": ("web_search", "search_news"),
        "web_fetch": ("fetch_url", "fetch_json"),
    }

    def category_rules(self) -> tuple[list[str], list[str], list[str]]:
        """The per-category levels, as ``(deny, ask, allow)`` rule strings.

        These levels are documented in ``SECURITY.md``, ``CONFIGURATION.md`` and
        ``QUICKSTART.md`` as igni's permission model, and until now nothing read
        them. They were interpreted by ``PermissionGuard``, which was never
        constructed anywhere in the codebase — so ``shell_execute: "deny"`` in a
        config file, or from ``--read-only``, had no effect at all.

        Emitted as ordinary rules so they flow through the same pipeline as the
        ``deny``/``ask``/``allow`` lists, appended after them so nothing
        explicit is displaced. Ordering does not grant exceptions, though: the
        evaluator treats deny as its safety floor and runs it first, so a
        category set to ``deny`` is not reopened by a matching allow rule.

        **Only categories someone actually set are emitted.** The schema
        defaults are left inert on purpose, and the reason is not caution for
        its own sake: ``web_search`` and ``web_fetch`` default to ``"allow"``,
        so emitting defaults would hand the model an explicit allow rule for the
        web under ``--strict`` — loosening a safety flag as a side effect of
        making config work. ``file_write`` and ``shell_execute`` default to
        ``"ask"``, which is already what an unmatched tool does, so emitting
        those would only add ``PERMISSION_REQUEST`` hook traffic.

        The effect is that a default install behaves exactly as before, and a
        config or flag that says ``deny`` finally means it.
        """
        deny: list[str] = []
        ask: list[str] = []
        allow: list[str] = []
        bucket = {"deny": deny, "ask": ask, "allow": allow}
        for category, functions in self.CATEGORY_FUNCTIONS.items():
            value = getattr(self, category, None)
            # Compared against the field default rather than ``model_fields_set``:
            # the settings loader populates every field, so "was it set" is true
            # for all of them and would have emitted ``web_search: allow`` under
            # ``--strict``, handing the web back to a flag whose whole job is to
            # take it away. Measured, not assumed — the probe showed exactly that
            # before this line changed.
            if value is None or value == type(self).model_fields[category].default:
                continue
            target = bucket.get(value)
            if target is None:
                continue
            target.extend(functions)
        return deny, ask, allow
