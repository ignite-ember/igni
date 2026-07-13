"""Session memory operations — reading and optimizing user memories."""

import logging
from typing import Any

from agno.agent import Agent
from agno.memory import MemoryManager
from agno.memory.strategies.types import MemoryOptimizationStrategyType

from ember_code.core.config.models import ModelRegistry
from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


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

    async def optimize(self) -> dict[str, Any]:
        """Optimize user memories using the summarize strategy.

        Returns a dict with before/after counts and token savings.
        """
        manager = self._create_manager()
        if not manager:
            return {"error": "Memory manager not available"}
        try:
            agent = self._create_reader_agent()
            before = await agent.aget_user_memories(user_id=self.user_id)
            count_before = len(before) if before else 0

            if count_before < 2:
                return {
                    "count_before": count_before,
                    "count_after": count_before,
                    "message": "Not enough memories to optimize",
                }

            optimized = await manager.aoptimize_memories(
                user_id=self.user_id,
                strategy=MemoryOptimizationStrategyType.SUMMARIZE,
                apply=True,
            )
            count_after = len(optimized) if optimized else 0

            return {
                "count_before": count_before,
                "count_after": count_after,
                "message": f"Optimized {count_before} memories into {count_after}",
            }
        except Exception as e:
            return {"error": str(e)}
