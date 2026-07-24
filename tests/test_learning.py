"""Tests for learning integration — LearningMachine creation from config."""

from agno.db.in_memory import InMemoryDb

from ember_code.core.config.settings import Settings
from ember_code.core.learn import LearnBootResult, create_learning_machine


class TestCreateLearningMachine:
    def test_disabled_returns_not_ok(self):
        """When learning.enabled is False, returns a not-ok envelope
        with an actionable reason string."""
        s = Settings()
        s.learning.enabled = False
        result = create_learning_machine(s)
        assert isinstance(result, LearnBootResult)
        assert result.ok is False
        assert result.machine is None
        assert result.reason == "disabled in settings"

    def test_enabled_no_db_returns_not_ok(self):
        """When learning is enabled but no db, returns a not-ok
        envelope naming the missing dependency."""
        s = Settings()
        s.learning.enabled = True
        result = create_learning_machine(s, db=None)
        assert result.ok is False
        assert result.machine is None
        assert result.reason == "no database configured"

    def test_enabled_with_db(self):
        """When learning is enabled with a db, returns an ok envelope
        wrapping a LearningMachine."""
        s = Settings()
        s.learning.enabled = True
        db = InMemoryDb()
        result = create_learning_machine(s, db=db)
        assert result.ok is True
        assert result.machine is not None
        assert result.reason == ""

    def test_config_flags_passed_through(self):
        """Config flags are forwarded to LearningMachine."""
        s = Settings()
        s.learning.enabled = True
        s.learning.user_profile = True
        s.learning.user_memory = False
        s.learning.session_context = True
        s.learning.entity_memory = True
        s.learning.learned_knowledge = False
        db = InMemoryDb()

        result = create_learning_machine(s, db=db)
        assert result.machine is not None
        lm = result.machine
        assert lm.user_profile is True
        assert lm.user_memory is False
        assert lm.session_context is True
        assert lm.entity_memory is True
        assert lm.learned_knowledge is False

    def test_all_disabled_flags(self):
        """Even with learning enabled, individual stores can be disabled."""
        s = Settings()
        s.learning.enabled = True
        s.learning.user_profile = False
        s.learning.user_memory = False
        s.learning.session_context = False
        s.learning.entity_memory = False
        s.learning.learned_knowledge = False
        db = InMemoryDb()

        result = create_learning_machine(s, db=db)
        assert result.ok is True
        lm = result.machine
        assert lm is not None
        assert lm.user_profile is False
        assert lm.user_memory is False
