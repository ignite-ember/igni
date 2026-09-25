"""LLM-behavior cluster of config schemas.

Groups memory, artifacts, learning, reasoning, evals, and guardrails
schemas — the knobs that control what the model does with context
and how it learns / reasons across turns.
"""

from __future__ import annotations

from agno.learn.config import LearningMode, UserMemoryConfig
from pydantic import BaseModel

from ember_code.core.paths import CONFIG_DIR


class MemoryConfig(BaseModel):
    add_memories_to_context: bool = True


class ArtifactsConfig(BaseModel):
    """Versioned project documents with derivation lineage.

    ``capture_reads`` is what makes lineage cheap to maintain: every
    artifact the agent retrieves is offered back as a candidate source
    on the next write. Turning it off does not disable lineage — it
    only means nothing is proposed, and every derivation has to be
    cited by hand or is lost.
    """

    enabled: bool = True
    collection_name: str = "igni_artifacts"
    max_results: int = 10
    # ── Git-shared artifacts ──────────────────────────────────────
    share: bool = True  # enable git-synced artifact sharing
    share_file: str = f"{CONFIG_DIR}/artifacts.yaml"  # relative to project root
    auto_sync: bool = True  # auto-sync on session start/end
    # ── Lineage ───────────────────────────────────────────────────
    capture_reads: bool = True
    # Unconfirmed candidates older than this are pruned. Capture is
    # deliberately generous, so without an expiry the review surface
    # fills with reads nobody will ever adjudicate — which is the
    # same as having no review surface.
    candidate_ttl_days: int = 30
    # Warn at session start when confirmed sources have moved under
    # their dependents. Off makes staleness a thing you have to
    # remember to ask about, which is the failure this exists to fix.
    warn_on_stale: bool = True


class KnowledgeConfig(ArtifactsConfig):
    """Deprecated alias for :class:`ArtifactsConfig`.

    Kept so a config file written before the rename still loads. It
    is a subclass rather than an assignment so ``isinstance`` checks
    in third-party plugins keep working.
    """


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
