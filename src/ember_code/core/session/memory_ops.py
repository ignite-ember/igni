"""Session memory operations — reading and optimizing user memories."""

import logging
from typing import Any

from agno.agent import Agent
from agno.memory import MemoryManager
from agno.memory.strategies.types import MemoryOptimizationStrategyType
from pydantic import BaseModel

from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


class MemoryOptimizeResult(BaseModel):
    """Result of :meth:`SessionMemoryManager.optimize`.

    Rule 1 / Pattern 3: callers check ``success`` and read the typed
    fields rather than probing for ``"error"`` keys on a raw dict.
    """

    success: bool = True
    message: str = ""
    error: str | None = None
    count_before: int = 0
    count_after: int = 0

    @classmethod
    def ok(cls, *, before: int, after: int, message: str) -> "MemoryOptimizeResult":
        return cls(success=True, message=message, count_before=before, count_after=after)

    @classmethod
    def fail(cls, error: str) -> "MemoryOptimizeResult":
        return cls(success=False, error=error)


class SessionMemoryManager:
    """Manages Agno-backed user memories for a session."""

    def __init__(self, db: Any, settings: Settings, user_id: str):
        self.db = db
        self.settings = settings
        self.user_id = user_id

    def _create_manager(self) -> Any | None:
        """Create an Agno MemoryManager for memory operations."""
        if not self.db:
            return None
        model = ModelRegistry(self.settings).get_model()
        return MemoryManager(model=model, db=self.db)

    def _create_reader_agent(self) -> Any:
        """Create a temporary agent for reading memories."""
        model = ModelRegistry(self.settings).get_model()
        return Agent(name="_memory_reader", model=model, db=self.db)

    async def get_memories(self) -> list[dict[str, str]]:
        """Get all user memories for the current user."""
        if not self.db:
            return []
        try:
            agent = self._create_reader_agent()
            memories = await agent.aget_user_memories(user_id=self.user_id)
            if not memories:
                return []
            return [
                {"memory": m.memory or "", "topics": ", ".join(m.topics or [])} for m in memories
            ]
        except Exception as exc:
            logger.debug("Failed to get user memories: %s", exc)
            return []

    async def optimize(self) -> MemoryOptimizeResult:
        """Optimize user memories using the summarize strategy."""
        manager = self._create_manager()
        if not manager:
            return MemoryOptimizeResult.fail("Memory manager not available")
        try:
            agent = self._create_reader_agent()
            before = await agent.aget_user_memories(user_id=self.user_id)
            count_before = len(before) if before else 0

            if count_before < 2:
                return MemoryOptimizeResult.ok(
                    before=count_before,
                    after=count_before,
                    message="Not enough memories to optimize",
                )

            optimized = await manager.aoptimize_memories(
                user_id=self.user_id,
                strategy=MemoryOptimizationStrategyType.SUMMARIZE,
                apply=True,
            )
            count_after = len(optimized) if optimized else 0

            return MemoryOptimizeResult.ok(
                before=count_before,
                after=count_after,
                message=f"Optimized {count_before} memories into {count_after}",
            )
        except Exception as exc:
            return MemoryOptimizeResult.fail(str(exc))
