"""LLM-behavior cluster of config schemas.

Groups memory, knowledge, learning, reasoning, evals, and guardrails
schemas — the knobs that control what the model does with context
and how it learns / reasons across turns.
"""

from __future__ import annotations

from agno.learn.config import LearningMode, UserMemoryConfig
from pydantic import BaseModel


class MemoryConfig(BaseModel):
    add_memories_to_context: bool = True


class KnowledgeConfig(BaseModel):
    enabled: bool = True
    collection_name: str = "ember_knowledge"
    max_results: int = 10
    # ── Git-shared knowledge ──────────────────────────────────────
    share: bool = True  # enable git-synced knowledge sharing
    share_file: str = ".ember/knowledge.yaml"  # path relative to project root
    auto_sync: bool = True  # auto-sync on session start/end


class LearningConfig(BaseModel):
    enabled: bool = True
    # Auto-extraction blobs that Agno's LearningMachine fires *after*
    # every run as separate LLM calls. They added 5–10 s to the tail
    # between ``streaming_done`` and ``run_completed`` (the user
    # perceives "still working" while the visible answer is already
    # done). We rely on the agentic ``user_memory`` path instead —
    # the agent calls ``update_user_memory`` itself when it decides
    # the turn was memorable — so the auto-extractions are dead
    # weight in our setup.
    user_profile: bool = False
    user_memory: bool = True
    session_context: bool = False
    entity_memory: bool = False
    learned_knowledge: bool = False

    def to_user_memory_input(self) -> bool | UserMemoryConfig:
        """Translate the ``user_memory`` flag to Agno's input union.

        User memory is agent-driven only. Default Agno behaviour is
        ``mode=ALWAYS`` which fires an extraction model call after
        *every* turn, even when nothing memorable was said. We want
        the agent to decide — when it learns something durable about
        the user (preferences, role, project conventions) it calls
        ``update_user_memory(task)`` itself; otherwise nothing extra
        happens. Single extraction call per agent decision, not
        periodic background activity.

        Returns ``False`` when disabled, and a fully-configured
        :class:`UserMemoryConfig` in AGENTIC mode when enabled. This
        is where the "AGENTIC not ALWAYS" policy lives — with the
        data it describes (Rule 6).
        """
        if not self.user_memory:
            return False
        return UserMemoryConfig(
            mode=LearningMode.AGENTIC,
            enable_agent_tools=True,
            agent_can_update_memories=True,
        )


class ReasoningConfig(BaseModel):
    enabled: bool = False
    add_instructions: bool = True
    add_few_shot: bool = False


class GuardrailsConfig(BaseModel):
    pii_detection: bool = True
    prompt_injection: bool = False
    moderation: bool = False


class EvalsConfig(BaseModel):
    judge_model: str = "MiniMax-M2.7"
    num_iterations: int = 3
    accuracy_threshold: float = 7.0
    timeout_per_case: int = 30
