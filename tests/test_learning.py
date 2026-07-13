"""Tests for learning integration — LearningMachine creation from config."""

from agno.db.in_memory import InMemoryDb

from ember_code.core.config.settings import Settings
from ember_code.core.learn import create_learning_machine


class TestCreateLearningMachine:
    def test_disabled_returns_none(self):
        """When learning.enabled is False, returns None."""
        s = Settings()
        s.learning.enabled = False
        assert create_learning_machine(s) is None

    def test_enabled_no_db_returns_none(self):
        """When learning is enabled but no db, returns None."""
        s = Settings()
        s.learning.enabled = True
        assert create_learning_machine(s, db=None) is None

    def test_enabled_with_db(self):
        """When learning is enabled with a db, returns a LearningMachine."""
        s = Settings()
        s.learning.enabled = True
        db = InMemoryDb()
        lm = create_learning_machine(s, db=db)
        assert lm is not None

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

        lm = create_learning_machine(s, db=db)
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

        lm = create_learning_machine(s, db=db)
        assert lm is not None
        assert lm.user_profile is False
        assert lm.user_memory is False
