"""``PermissionsConfig`` — the ``permissions`` block of ``Settings``.

Extracted from :mod:`ember_code.core.config.settings`. Pure data
schema — no methods.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

#: The shell tool every git invocation arrives as. igni has no git tool and no
#: read tool — a commit, a push and a ``cat`` are all ``run_shell_command`` — so
#: a git category can only be carried by the command string, not by a function
#: name the way ``file_write`` is.
_SHELL_FUNCTION = "run_shell_command"


def _shell_rules(*commands: str) -> tuple[str, ...]:
    """``"git push"`` → the rule strings that match it as a real command.

    Two patterns per command because the evaluator fnmatches the whole
    ``command`` string and fnmatch has no word boundaries: ``git push*``
    catches the command run on its own, ``* git push*`` catches it after a
    separator (``cd repo && git push``, ``sudo git push``). Neither is exact —
    ``echo git push`` matches the second — and that asymmetry is deliberate:
    over-matching sends a call to the ask/deny path, under-matching lets a
    force-push run unseen, and only one of those is recoverable.
    """
    rules: list[str] = []
    for command in commands:
        rules.append(f"{_SHELL_FUNCTION}({command}*)")
        rules.append(f"{_SHELL_FUNCTION}(* {command}*)")
    return tuple(rules)


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

    #: ``safety.require_confirmation``, copied here by ``Settings`` so
    #: :meth:`category_rules` can emit it. The list lives in the ``safety``
    #: block — where MIGRATION.md tells arrivals from Claude Code to put what
    #: used to be ``"ask": ["Bash(git push *)"]`` — but the only thing that
    #: reads permission rules is the evaluator built from *this* block, so the
    #: list had to reach it or keep meaning nothing.
    #:
    #: ``exclude=True`` matters: ``Settings.default_dict()`` seeds the merge
    #: accumulator, so a field carrying the eight shipped defaults would bake
    #: them into the merge base and a user who set ``require_confirmation: []``
    #: could never take them back out.
    confirm_commands: list[str] = Field(default_factory=list, exclude=True)

    #: What each per-category level governs, as permission-rule strings.
    #: Function-name categories use the registry's own names rather than a
    #: hand-written list, so a renamed function does not quietly drop out of
    #: its category — the failure this whole mapping exists to end.
    #:
    #: The two git rows are argument patterns rather than function names,
    #: because there is no git *tool* to name (see ``_shell_rules``). They were
    #: the two categories PRD-21 left unwired, and the two most likely to be
    #: set deliberately: ``--auto-approve`` sets both to ``allow`` and
    #: ``--strict`` sets both to ``deny``, and until now neither did anything.
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
        "git_push": _shell_rules("git push"),
        # Destructive means "loses work that is not recoverable from this
        # clone": a discarded working tree, a deleted branch, a dropped stash,
        # a rewritten remote history. Ordinary history rewriting that keeps the
        # objects (``git rebase``, ``git commit --amend``) is left out — it is
        # recoverable through the reflog, and a category that fires on every
        # rebase gets turned off.
        "git_destructive": _shell_rules(
            "git push --force",
            "git push -f",
            "git reset --hard",
            "git clean",
            "git checkout --",
            "git branch -D",
            "git stash drop",
            "git stash clear",
            "git filter-branch",
        ),
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

        ``safety.require_confirmation`` rides along in the ask bucket — see
        :meth:`_confirmation_ask_rules` for why it is here and what it does not
        promise.

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
        ask.extend(self._confirmation_ask_rules())
        return deny, ask, allow

    def _confirmation_ask_rules(self) -> list[str]:
        """``safety.require_confirmation`` as ask rules, or nothing in ``dontAsk``.

        MIGRATION.md tells someone arriving from Claude Code that this list is
        where ``"ask": ["Bash(git push *)"]`` goes. It was read by no code in
        ``src``, so following the migration guide moved a working ask rule into
        a field that did nothing — including for ``git push --force``,
        ``terraform apply`` and ``kubectl delete``. These rules are exactly the
        ask rules the user would otherwise have hand-written, and no more: an
        ask rule loses to the mode step, so ``/bypass`` still bypasses this.
        That is the pipeline's documented contract, not a gap opened here.

        Nothing is emitted in ``dontAsk``, and that is the one place this
        differs from a hand-written rule. An ask rule there is not a prompt,
        it is a pass — the evaluator stage fires ``PERMISSION_REQUEST`` and
        continues — while an *unmatched* tool in ``dontAsk`` is denied at step
        6. Since the shipped list is non-empty by default, emitting it would
        have turned ``terraform apply`` and ``kubectl delete`` from denied into
        allowed on every ``--strict`` and headless run. With nobody there to
        confirm, "requires confirmation" reads as "no", not "yes".
        """
        if self.mode == "dontAsk":
            return []
        return list(_shell_rules(*self.confirm_commands))
