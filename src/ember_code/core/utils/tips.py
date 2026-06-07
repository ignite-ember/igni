"""Tips — contextual usage tips shown at session start.

Tips are not random — they look at the current config and session state
and surface the most relevant suggestion.  This makes tips feel like a
helpful nudge rather than noise.
"""

import logging
import random
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings

# ── Contextual tips ─────────────────────────────────────────────────
# Each tip is a (condition, message) pair.  ``condition`` receives the
# Settings object and project dir and returns True when the tip is
# relevant.  Tips that match the current state are prioritised; if
# none match, a random general tip is shown.


def _no_ember_md(settings: "Settings", project_dir: Path) -> bool:
    return not (project_dir / settings.context.project_file).exists()


def _knowledge_disabled(settings: "Settings", _p: Path) -> bool:
    return not settings.knowledge.enabled


def _knowledge_enabled_no_share(settings: "Settings", _p: Path) -> bool:
    return settings.knowledge.enabled and not settings.knowledge.share


def _guardrails_off(settings: "Settings", _p: Path) -> bool:
    g = settings.guardrails
    return not g.pii_detection and not g.prompt_injection and not g.moderation


def _reasoning_off(settings: "Settings", _p: Path) -> bool:
    return not settings.reasoning.enabled


def _learning_off(settings: "Settings", _p: Path) -> bool:
    return not settings.learning.enabled


def _no_custom_agents(settings: "Settings", project_dir: Path) -> bool:
    agent_dir = project_dir / ".ember" / "agents"
    if not agent_dir.exists():
        return True
    return len(list(agent_dir.glob("*.md"))) == 0


def _web_denied(settings: "Settings", _p: Path) -> bool:
    return settings.permissions.web_search == "deny"


CONTEXTUAL_TIPS: list[tuple[Callable, str]] = [
    (
        _no_ember_md,
        "Create an ember.md in your project root to give agents project-specific context.",
    ),
    (
        _knowledge_disabled,
        'Enable the knowledge base with "knowledge.enabled: true" to store and search documents.',
    ),
    (
        _knowledge_enabled_no_share,
        'Turn on "knowledge.share: true" to sync knowledge to git so your team gets it too.',
    ),
    (
        _guardrails_off,
        "Enable guardrails (PII detection, prompt injection, moderation) for safer agent execution.",
    ),
    (
        _reasoning_off,
        'Set "reasoning.enabled: true" to give agents step-by-step thinking tools.',
    ),
    (
        _learning_off,
        'Set "learning.enabled: true" so agents learn your preferences across sessions.',
    ),
    (
        _no_custom_agents,
        "Drop a .md file in .ember/agents/ to create a project-specific agent — no code needed.",
    ),
    (
        _web_denied,
        'Install ember-code[web] and set "web_search: allow" to let agents search the web.',
    ),
]

# General tips shown when no contextual tip matches
GENERAL_TIPS: list[str] = [
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


def get_tip(settings: "Settings | None" = None, project_dir: Path | None = None) -> str:
    """Return a contextual tip based on the current config and project state.

    Checks each contextual tip's condition against the settings. If any
    match, one is picked at random from the matching set. Otherwise a
    random general tip is returned.
    """
    if settings is None or project_dir is None:
        return random.choice(GENERAL_TIPS)

    matching = []
    for condition, message in CONTEXTUAL_TIPS:
        try:
            if condition(settings, project_dir):
                matching.append(message)
        except Exception as exc:
            logger.debug("Tip condition check failed: %s", exc)
            continue

    if matching:
        return random.choice(matching)

    return random.choice(GENERAL_TIPS)


def random_tip() -> str:
    """Return a random general tip (no config context)."""
    return random.choice(GENERAL_TIPS)
