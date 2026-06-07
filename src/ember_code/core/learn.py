"""Learning integration — creates an Agno LearningMachine from config."""

import logging
from typing import Any

from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


def create_learning_machine(settings: Settings, db: Any | None = None) -> Any | None:
    """Create an Agno LearningMachine if learning is enabled.

    The LearningMachine uses the same db as the session so all learning
    data is co-located with session history and memories.

    Returns None if learning is disabled or dependencies are missing.
    """
    if not settings.learning.enabled:
        return None

    if db is None:
        logger.warning("Learning enabled but no database configured — skipping")
        return None

    try:
        from agno.learn import LearningMachine
        from agno.learn.config import LearningMode, UserMemoryConfig
    except ImportError:
        logger.warning("agno.learn not available — learning disabled")
        return None

    # User memory: agent-driven only. Default Agno behaviour is
    # ``mode=ALWAYS`` which fires an extraction model call after
    # *every* turn, even when nothing memorable was said. We want the
    # agent to decide — when it learns something durable about the
    # user (preferences, role, project conventions) it calls
    # ``update_user_memory(task)`` itself; otherwise nothing extra
    # happens. Single extraction call per agent decision, not
    # periodic background activity.
    user_memory_input: bool | UserMemoryConfig = False
    if settings.learning.user_memory:
        user_memory_input = UserMemoryConfig(
            mode=LearningMode.AGENTIC,
            enable_agent_tools=True,
            agent_can_update_memories=True,
        )

    try:
        lm = LearningMachine(
            db=db,
            user_profile=settings.learning.user_profile,
            user_memory=user_memory_input,
            session_context=settings.learning.session_context,
            entity_memory=settings.learning.entity_memory,
            learned_knowledge=settings.learning.learned_knowledge,
        )
        logger.info(
            "LearningMachine created (profile=%s, memory=%s/agentic, context=%s, entity=%s)",
            settings.learning.user_profile,
            settings.learning.user_memory,
            settings.learning.session_context,
            settings.learning.entity_memory,
        )
        return lm
    except Exception as e:
        logger.warning("Failed to create LearningMachine: %s", e)
        return None
