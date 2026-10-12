"""The git categories and the confirm list decide something.

Two settings that read as policy and were read by nothing:

* ``permissions.git_push`` / ``permissions.git_destructive`` existed as fields
  and had no row in ``CATEGORY_FUNCTIONS``, so ``category_rules()`` walked past
  them. They are the two categories ``--auto-approve`` and ``--strict`` set
  explicitly, which makes "silently dropped" the worst possible shape for them:
  ``--strict`` announced ``git_push: deny`` and denied no push.

* ``safety.require_confirmation`` — eight commands including
  ``git push --force``, ``terraform apply`` and ``kubectl delete`` — was read by
  no code in ``src``. MIGRATION.md points arrivals from Claude Code at it as the
  home for what used to be ``"ask": ["Bash(git push *)"]``, so following the
  guide moved a working rule into a dead field.

Every assertion here fails on the pre-fix tree: ``category_rules()`` returned
``([], [], [])`` for a config with ``git_push="deny"`` set, and a default
``Settings`` produced ``DEFER`` rather than ``ASK`` for ``git push``.
"""

from __future__ import annotations

from ember_code.core.config.permission_eval import PermissionEvaluator
from ember_code.core.config.schemas.permissions import PermissionsConfig
from ember_code.core.config.settings import Settings


def _decide(perms: PermissionsConfig, command: str) -> str:
    """Mirror exactly what ``ToolEventHookFactory.permission_evaluator`` builds."""
    cat_deny, cat_ask, cat_allow = perms.category_rules()
    evaluator = PermissionEvaluator.from_strings(
        mode=perms.mode,
        deny=[*perms.deny, *cat_deny],
        ask=[*perms.ask, *cat_ask],
        allow=[*perms.allow, *cat_allow],
    )
    return evaluator.evaluate("run_shell_command", {"command": command}).name


class TestTheGitCategoriesAreWired:
    def test_denying_pushes_denies_a_push(self):
        assert _decide(PermissionsConfig(git_push="deny"), "git push origin main") == "DENY"

    def test_a_push_after_a_separator_is_still_a_push(self):
        """fnmatch has no word boundaries, so the rule set carries the
        ``cd repo && git push`` form explicitly. Without it the category is a
        speed bump — one ``cd`` and it is gone."""
        assert _decide(PermissionsConfig(git_push="deny"), "cd repo && git push") == "DENY"

    def test_denying_destructive_git_denies_the_destructive_ones(self):
        perms = PermissionsConfig(git_destructive="deny")

        assert _decide(perms, "git reset --hard HEAD~3") == "DENY"
        assert _decide(perms, "git clean -fdx") == "DENY"
        assert _decide(perms, "git push --force origin main") == "DENY"
        assert _decide(perms, "git branch -D release") == "DENY"

    def test_ordinary_git_is_untouched_by_the_destructive_category(self):
        """A category that swallows ``git status`` gets turned off, which
        costs more than it ever bought."""
        perms = PermissionsConfig(git_destructive="deny")

        assert _decide(perms, "git status") != "DENY"
        assert _decide(perms, "git commit -m 'x'") != "DENY"

    def test_the_git_categories_are_real_fields(self):
        for category in ("git_push", "git_destructive"):
            assert category in PermissionsConfig.CATEGORY_FUNCTIONS
            assert category in PermissionsConfig.model_fields

    def test_a_default_config_still_emits_nothing(self):
        """The existing contract: only a level that differs from its default
        becomes a rule, so a default install is unchanged."""
        assert PermissionsConfig().category_rules() == ([], [], [])


class TestRequireConfirmationReachesTheEvaluator:
    def test_the_shipped_list_asks(self):
        settings = Settings()

        assert _decide(settings.permissions, "git push --force origin main") == "ASK"
        assert _decide(settings.permissions, "terraform apply") == "ASK"
        assert _decide(settings.permissions, "kubectl delete pod web-0") == "ASK"

    def test_an_unlisted_command_is_not_swept_up(self):
        assert _decide(Settings().permissions, "ls -la") == "DEFER"

    def test_emptying_the_list_empties_the_rules(self):
        """The field has to remain a setting, not a constant.

        This is why the carried copy is excluded from ``model_dump``: it would
        otherwise land in ``Settings.default_dict()``, become part of the merge
        base every config is merged onto, and leave a user who wrote
        ``require_confirmation: []`` with the eight defaults anyway.
        """
        settings = Settings(safety={"require_confirmation": []})

        assert settings.permissions.confirm_commands == []
        assert _decide(settings.permissions, "terraform apply") == "DEFER"
        assert "confirm_commands" not in Settings.default_dict()["permissions"]

    def test_a_user_supplied_command_asks(self):
        settings = Settings(safety={"require_confirmation": ["rsync"]})

        assert _decide(settings.permissions, "rsync -a src/ dst/") == "ASK"

    def test_headless_denies_rather_than_asking(self):
        """The one place this differs from a hand-written ask rule.

        In ``dontAsk`` an ask rule is not a prompt, it is a pass: the evaluator
        stage fires ``PERMISSION_REQUEST`` and continues, while an *unmatched*
        tool is denied at step 6. Emitting the shipped list there would have
        turned ``kubectl delete`` from denied into allowed on every ``--strict``
        run — so with nobody to confirm, "requires confirmation" reads as no.
        """
        settings = Settings(permissions={"mode": "dontAsk"})

        assert _decide(settings.permissions, "kubectl delete pod web-0") == "DENY"
        assert settings.permissions.category_rules()[1] == []
