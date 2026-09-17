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
  The constructor's explanation went to a private
  ``Session._knowledge_error`` that no reader had looked at since the
  subsystem-registry rewrite, so the branch that exists to explain the
  state could not run. It is recorded on ``session.subsystems`` now.

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

import inspect
from types import SimpleNamespace

import pytest

from ember_code.backend.cmd_knowledge import KnowledgeCommand
from ember_code.backend.schemas_config import ConfigView
from ember_code.backend.subsystem_status import KNOWLEDGE, SubsystemState
from ember_code.core.config.schemas.storage_and_paths import StorageConfig
from ember_code.core.session import Session


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
        """Against the session's own settings, not a fresh ``StorageConfig()``.

        The default is resolved per instance — it reads the filesystem to
        decide between the pre- and post-rename home (see
        ``storage_and_paths._home_config_path``) — so a config built inside the
        test and one built when this module was imported can legitimately name
        different directories. Comparing the card to a fresh default therefore
        tests the clock, not the card. What the card owes the user is the
        directory *this session* is using.
        """
        session = _Session()
        text = ConfigView.from_session(session, store=_NoCreds()).to_command_result().content

        assert "**Storage:**" in text
        assert session.settings.storage.data_dir in text

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


class _DeferredKnowledgeSession:
    """The slice of ``Session`` that ``_init_knowledge`` writes and
    ``KnowledgeCommand.panel`` reads.

    ``Session.__init__`` needs a project on disk, a db, a model and an
    agent build; ``_init_knowledge`` needs three attributes. Calling the
    one method unbound is what keeps this a unit test of the thing that
    regressed rather than a session-construction integration test.
    """

    class _Mgr:
        async def status(self):
            return SimpleNamespace(enabled=False)

    def __init__(self, *, knowledge_enabled: bool = True) -> None:
        self.settings = SimpleNamespace(knowledge=SimpleNamespace(enabled=knowledge_enabled))
        self.knowledge_mgr = self._Mgr()
        Session._init_knowledge(self, self.settings, None)


class TestTheDeferredKnowledgeStateExplainsItself:
    """``/knowledge`` said "Knowledge base failed to initialize." on a
    session that had not failed at anything.

    The constructor did write an explanation, to a private
    ``_knowledge_error`` attribute — and the subsystem-registry rewrite
    had already moved every reader (``Session.knowledge_error``,
    ``KnowledgeCommand.panel``) onto ``session.subsystems``, so the
    write reached nobody. These tests read the reason the way the
    product reads it; the previous three read it out of ``core.py``'s
    source text, which is exactly how a write with no reader passes for
    a working diagnostic.
    """

    def test_the_registry_records_the_deferred_state(self):
        session = _DeferredKnowledgeSession()

        entry = session.subsystems.get(KNOWLEDGE)

        assert entry is not None, (
            "nothing recorded why knowledge is absent, so every reader "
            "falls through to the causeless fallback"
        )
        # PREPARING, not FAILED: waiting for a runtime is the ordinary
        # startup case, and calling it a failure is the original lie.
        assert entry.state is SubsystemState.PREPARING
        assert "Neo4j" in entry.reason
        assert "/codeindex" in entry.fix

    @pytest.mark.asyncio
    async def test_the_panel_names_the_cause(self):
        """The sentence a user actually sees."""
        result = await KnowledgeCommand(_DeferredKnowledgeSession()).panel()

        assert "failed to initialize" not in result.content
        assert "Neo4j" in result.content

    def test_attaching_a_runtime_clears_it(self):
        """Otherwise a working session keeps being told to go and start
        the runtime it is already using."""
        session = _DeferredKnowledgeSession()

        session.subsystems.set(KNOWLEDGE, SubsystemState.READY)

        assert session.subsystems.get(KNOWLEDGE).state is SubsystemState.READY
        assert Session.knowledge_error.fget(session) is None

    def test_attach_knowledge_neo4j_is_what_clears_it(self):
        """The clear has to live in ``attach_knowledge_neo4j``; a session
        that got its runtime must stop reporting that it has none."""
        source = inspect.getsource(Session.attach_knowledge_neo4j)

        assert "SubsystemState.READY" in source
