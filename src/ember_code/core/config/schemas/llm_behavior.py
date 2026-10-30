"""LLM-behavior cluster of config schemas.

Groups memory, learning, reasoning, evals, and guardrails
schemas — the knobs that control what the model does with context
and how it learns / reasons across turns.
"""

from __future__ import annotations

from agno.learn.config import LearningMode, UserMemoryConfig
from pydantic import BaseModel


class MemoryConfig(BaseModel):
    add_memories_to_context: bool = True


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
    """Defaults the eval loader / runner apply when a case omits a value.

    Every field here is read: ``judge_model`` by
    :meth:`SuiteRunner._lookup_judge_model`, ``num_iterations`` and
    ``accuracy_threshold`` by the YAML loader (as the per-case
    fallbacks), and ``timeout_per_case`` by :class:`CaseRunner` as the
    fallback for a case with no ``case_timeout:`` of its own.
    """

    judge_model: str = "MiniMax-M2.7"
    num_iterations: int = 3
    accuracy_threshold: float = 7.0
    # Fallback per-case wall-clock budget, in seconds. The old value
    # here was 30, which nothing read — every committed suite that
    # cares pins ``case_timeout:`` between 120 and 900. Now that the
    # runner honours this field, 30 would abort essentially every real
    # agent case before it finished, so the default is the order of
    # magnitude those suites actually live at. Set to 0 to disable the
    # default timeout entirely (per-case ``case_timeout`` still applies).
    timeout_per_case: int = 600
