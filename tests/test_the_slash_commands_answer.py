"""Three slash commands that answered wrongly, found by running them.

Every one was reachable from the composer's menu, and none had a test.
They were found the only way this kind of thing is found — by typing
each of the 31 commands into a live app and reading what came back.

* ``/config`` raised. ``ConfigView`` read ``settings.storage.backend``,
  a field ``StorageConfig`` no longer has — choosing a storage backend
  went away with the backward-compat cleanup and the renderer did not.
  The user saw ``session routing failed: 'StorageConfig' object has no
  attribute 'backend'``. Same shape as the ``/ctx`` defect: an
  attribute error from a view model, surfaced through the message
  dispatcher's generic catch as a routing failure.

* ``/knowledge`` said "Knowledge base failed to initialize." — no
  cause, and not true. Nothing failed. The index is *deferred* until a
  Neo4j runtime attaches, and this session never got one.
  ``Session._knowledge_error`` was initialised to ``None`` and
  assigned nowhere in the codebase, so the branch that exists to
  explain the failure could not run.

* ``/plugin`` did nothing at all — no output, no panel, not even the
  echoed command line. That one is in ``Composer.test.ts``; the
  composer highlighted ``/plugins`` when you typed ``/plugin``, and
  Enter compared the typed text against the highlighted entry.

The pattern across all three: a command whose *output* nobody had
read. Two of the three fail in a way that looks like infrastructure
("routing failed", "failed to initialize") rather than like the small
code defect each actually is, which is why they survived — the error
text pointed away from the file with the bug in it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ember_code.backend.schemas_config import ConfigView
from ember_code.core.config.schemas.storage_and_paths import StorageConfig

_ROOT = Path(__file__).resolve().parent.parent


class _Settings:
    """The slice of ``Session.settings`` that ``ConfigView`` reads."""

    class models:
        default = "MiniMax-M2.7"

    class permissions:
        file_write = "ask"
        shell_execute = "ask"

    storage = StorageConfig()

    class learning:
        enabled = True

    class reasoning:
        enabled = False

    class guardrails:
        pii_detection = True
        prompt_injection = False
        moderation = False

    class knowledge:
        enabled = True

    class orchestration:
        max_total_agents = 8
        max_nesting_depth = 3


class _Session:
    settings = _Settings()
    session_id = "abc123"


class _NoCreds:
    """A credentials store that finds nothing — the offline case."""

    def load(self):
        class _Result:
            ok = False
            creds = None

        return _Result()


class TestConfigRenders:
    def test_from_session_does_not_raise(self):
        """The regression. This threw ``AttributeError`` for every
        user, every time, and the message they saw named neither the
        command nor the field."""
        view = ConfigView.from_session(_Session(), store=_NoCreds())

        assert view.model == "MiniMax-M2.7"

    def test_it_reads_a_field_storage_config_actually_has(self):
        """Stated against the schema rather than against the view.

        The defect was a reference to a field that had been deleted, so
        the check that matters is that whatever ``/config`` reads is
        still in ``StorageConfig``. A test that only called
        ``from_session`` would go green again the moment someone
        reintroduced a differently-broken attribute.
        """
        assert "data_dir" in StorageConfig.model_fields
        assert "backend" not in StorageConfig.model_fields

    def test_the_card_names_where_data_lives(self):
        text = ConfigView.from_session(_Session(), store=_NoCreds()).to_command_result().content

        assert "**Storage:**" in text
        assert StorageConfig().data_dir in text

    def test_the_card_is_not_an_error(self):
        """How the user met it: a red line beginning "session routing
        failed". Asserted on the rendered text because that is what
        reached them."""
        text = ConfigView.from_session(_Session(), store=_NoCreds()).to_command_result().content

        assert text.startswith("## Configuration")
        assert "routing failed" not in text
        assert "has no attribute" not in text

    @pytest.mark.parametrize(
        "field",
        ["Model", "Auth", "Permissions", "Storage", "Session"],
    )
    def test_every_headline_field_is_present(self, field: str):
        """A renderer that silently drops a line is the failure this
        class cannot otherwise see: ``from_session`` succeeding says
        nothing about the card being complete."""
        text = ConfigView.from_session(_Session(), store=_NoCreds()).to_command_result().content

        assert f"**{field}:**" in text


class TestTheKnowledgeErrorIsAssigned:
    """``_knowledge_error`` had exactly two mentions: the ``= None``
    that created it and the property that read it.

    That is a whole diagnostic path — a documented one, with its own
    branch in ``KnowledgeCommand.panel`` — that could not execute. A
    grep is the honest test for it: the point is not that some
    particular call site sets the field, it is that *something* does.
    """

    def _core(self) -> str:
        return (_ROOT / "src/ember_code/core/session/core.py").read_text()

    def test_something_assigns_it(self):
        assignments = re.findall(r"^\s*self\._knowledge_error\s*=", self._core(), re.M)

        # Three: the initial None, the deferred explanation, and the
        # clear when a runtime finally attaches.
        assert len(assignments) >= 2, (
            "_knowledge_error is initialised and never set, so "
            "KnowledgeCommand.panel's error branch cannot run and every "
            "failure reports the causeless fallback."
        )

    def test_the_deferred_case_explains_itself(self):
        """The commonest cause, and the one that is not a failure.

        The text has to name Neo4j and point at ``/codeindex``, because
        "failed to initialize" sent people looking for a crash that had
        not happened.
        """
        core = self._core()
        deferred = core[
            core.index("Knowledge: deferred") - 1200 : core.index("Knowledge: deferred")
        ]

        assert "Neo4j runtime" in deferred
        assert "/codeindex" in deferred

    def test_attaching_a_runtime_clears_it(self):
        """Otherwise a working session keeps being told to go and start
        the runtime it is already using."""
        core = self._core()
        attach = core[core.index("Knowledge: switched to neo4j backend") - 900 :]

        assert "self._knowledge_error = None" in attach.split("logger.info")[0]
