"""Tips — contextual usage tips shown at session start.

Tips are not random — they look at the current config and session state
and surface the most relevant suggestion.  This makes tips feel like a
helpful nudge rather than noise.

Architecture: a :class:`TipRegistry` owns a list of :class:`ContextualTip`
subclasses (each subclass owns one predicate + one message) plus a list
of general strings.  ``get_tip`` / ``random_tip`` remain as module-level
delegates for backward compatibility with existing callers and tests.
"""

import logging
import random
from abc import abstractmethod
from functools import cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


# ── Context passed to every contextual predicate ────────────────────


class TipContext(BaseModel):
    """The shared subject every contextual tip inspects.

    Bundles the two arguments the eight predicates used to accept as
    a state-first tuple (``settings``, ``project_dir``).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    project_dir: Path


# ── Contextual tip base + concrete subclasses ───────────────────────


class ContextualTip(BaseModel):
    """A tip whose relevance depends on the current :class:`TipContext`.

    Concrete subclasses override :meth:`matches` and set ``id`` /
    ``message`` as class-level defaults.  Selection is polymorphic —
    :class:`TipRegistry` iterates a ``list[ContextualTip]`` and lets each
    subclass answer for itself, replacing the former dispatch-list of
    ``(callable, message)`` tuples.
    """

    id: str
    message: str

    @abstractmethod
    def matches(self, ctx: TipContext) -> bool:
        """Return True when this tip is relevant to ``ctx``."""


class NoEmberMdTip(ContextualTip):
    id: str = "no_ember_md"
    message: str = (
        "Create an ember.md in your project root to give agents project-specific context."
    )

    def matches(self, ctx: TipContext) -> bool:
        return not (ctx.project_dir / ctx.settings.context.project_file).exists()


class KnowledgeDisabledTip(ContextualTip):
    id: str = "knowledge_disabled"
    message: str = (
        'Enable the knowledge base with "knowledge.enabled: true" to store and search documents.'
    )

    def matches(self, ctx: TipContext) -> bool:
        return not ctx.settings.knowledge.enabled


class KnowledgeNoShareTip(ContextualTip):
    id: str = "knowledge_no_share"
    message: str = (
        'Turn on "knowledge.share: true" to sync knowledge to git so your team gets it too.'
    )

    def matches(self, ctx: TipContext) -> bool:
        k = ctx.settings.knowledge
        return k.enabled and not k.share


class GuardrailsOffTip(ContextualTip):
    id: str = "guardrails_off"
    message: str = (
        "Enable guardrails (PII detection, prompt injection, moderation) for safer agent execution."
    )

    def matches(self, ctx: TipContext) -> bool:
        g = ctx.settings.guardrails
        return not g.pii_detection and not g.prompt_injection and not g.moderation


class ReasoningOffTip(ContextualTip):
    id: str = "reasoning_off"
    message: str = 'Set "reasoning.enabled: true" to give agents step-by-step thinking tools.'

    def matches(self, ctx: TipContext) -> bool:
        return not ctx.settings.reasoning.enabled


class LearningOffTip(ContextualTip):
    id: str = "learning_off"
    message: str = 'Set "learning.enabled: true" so agents learn your preferences across sessions.'

    def matches(self, ctx: TipContext) -> bool:
        return not ctx.settings.learning.enabled


class NoCustomAgentsTip(ContextualTip):
    id: str = "no_custom_agents"
    message: str = (
        "Drop a .md file in .ember/agents/ to create a project-specific agent — no code needed."
    )

    def matches(self, ctx: TipContext) -> bool:
        agent_dir = ctx.project_dir / ".ember" / "agents"
        if not agent_dir.exists():
            return True
        return len(list(agent_dir.glob("*.md"))) == 0


class WebDeniedTip(ContextualTip):
    id: str = "web_denied"
    message: str = (
        'Install ember-code[web] and set "web_search: allow" to let agents search the web.'
    )

    def matches(self, ctx: TipContext) -> bool:
        return ctx.settings.permissions.web_search == "deny"


# ── Registry (owns the catalogs + selection policy) ─────────────────


class TipRegistry(BaseModel):
    """Owns the catalog of contextual + general tips and picks one.

    Replaces the former module-level ``CONTEXTUAL_TIPS`` /
    ``GENERAL_TIPS`` globals plus the ``get_tip`` free function.
    Additional tips can be registered at runtime via :meth:`register`
    (enables the "plugins ship their own tips" scenario).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    contextual: list[ContextualTip]
    general: list[str]

    # General tips shown when no contextual tip matches.
    _DEFAULT_GENERAL: ClassVar[list[str]] = [
        'Use "/agents" to see all loaded agents and their tools.',
        "The Orchestrator picks the best team mode automatically. Just describe your task.",
        'Use "--verbose" to see which agents and team mode the Orchestrator picks.',
        'Use "/knowledge add <url|path|text>" to add content to your knowledge base.',
        'Use "/sync-knowledge" to manually sync knowledge between git and the vector DB.',
        "Agents can spawn sub-teams on the fly — no depth limit.",
        'Resume a previous session with "ignite-ember --resume".',
        'Use "/sessions" to browse and resume past sessions.',
        'Use "/config" to see your current settings at a glance.',
        "Use \"ignite-ember -m 'your task'\" for quick non-interactive tasks.",
        "Pipe mode reads stdin: cat file.log | ignite-ember -p -m 'explain this'.",
        'Use "/memory" to see what the agent remembers about you across sessions.',
        'Skills are reusable workflows — try "/commit" or "/resolve-issues".',
    ]

    @classmethod
    def default(cls) -> "TipRegistry":
        """Build a registry populated with the shipped tip catalog."""
        return cls(
            contextual=[
                NoEmberMdTip(),
                KnowledgeDisabledTip(),
                KnowledgeNoShareTip(),
                GuardrailsOffTip(),
                ReasoningOffTip(),
                LearningOffTip(),
                NoCustomAgentsTip(),
                WebDeniedTip(),
            ],
            general=list(cls._DEFAULT_GENERAL),
        )

    def register(self, tip: ContextualTip) -> None:
        """Append a runtime-provided contextual tip to the registry."""
        self.contextual.append(tip)

    def pick(self, ctx: TipContext | None) -> str:
        """Return the best-matching tip for ``ctx`` (or a general fallback).

        Mirrors the previous ``get_tip`` orchestration: every contextual
        tip whose :meth:`matches` returns True contributes its message
        to a candidate list; a random one is chosen.  If none match (or
        ``ctx`` is None), a random general tip is returned.
        """
        if ctx is None:
            return self.random_general()

        matching = [tip.message for tip in self.contextual if self._safe_match(tip, ctx)]
        if matching:
            return random.choice(matching)
        return self.random_general()

    def random_general(self) -> str:
        """Return a random general (context-free) tip."""
        return random.choice(self.general)

    def _safe_match(self, tip: ContextualTip, ctx: TipContext) -> bool:
        """Evaluate ``tip.matches(ctx)`` treating predicate failure as non-match.

        A broken predicate must not crash session start, but the
        swallowed exception is logged at ``debug`` with the tip's ``id``
        *and* class name plus the exception type/message so regressions
        stay diagnosable (traceable-swallow rather than silent-swallow).
        """
        try:
            return tip.matches(ctx)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.debug(
                "Tip %s (%s) predicate failed: %s: %s",
                tip.id,
                type(tip).__name__,
                type(exc).__name__,
                exc,
            )
            return False


# ── Backward-compat surface for the existing caller + tests ─────────
# TODO(audit-followup): migrate `interactive_loop` and
# `tests/test_tips_and_updates.py` to use ``TipRegistry.default()``
# directly and delete the module-level aliases below.


@cache
def _default_registry() -> TipRegistry:
    """Lazily-built shared registry; instance state, not module state."""
    return TipRegistry.default()


#: Deprecated alias — snapshot copy so mutations to the default
#: registry don't leak through. Prefer ``TipRegistry.default().contextual``.
CONTEXTUAL_TIPS: tuple[ContextualTip, ...] = tuple(_default_registry().contextual)

#: Deprecated alias — snapshot copy. Prefer ``TipRegistry.default().general``.
GENERAL_TIPS: tuple[str, ...] = tuple(_default_registry().general)


def get_tip(settings: Settings | None = None, project_dir: Path | None = None) -> str:
    """Return a contextual tip based on the current config and project state.

    Thin delegate to :meth:`TipRegistry.pick`.  Kept module-level so the
    existing caller in ``core/session/interactive_loop.py`` needs no
    change.
    """
    ctx = (
        TipContext(settings=settings, project_dir=project_dir)
        if settings is not None and project_dir is not None
        else None
    )
    return _default_registry().pick(ctx)


def random_tip() -> str:
    """Return a random general tip (no config context).

    Thin delegate to :meth:`TipRegistry.random_general`.
    """
    return _default_registry().random_general()
