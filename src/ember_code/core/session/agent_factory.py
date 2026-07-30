"""Agno-agent construction helpers.

Small factory functions that turn ember-code ``Settings`` into
Agno constructor arguments. Extracted from
:mod:`ember_code.core.session.core` so the god-file has fewer
top-level defs and these building blocks can be tested and
imported independently.

Both helpers degrade to ``None`` when the underlying Agno
sub-package isn't installed — the caller passes the ``None``
through to Agno's ``Agent(reasoning_tools=None, ...)`` /
``Agent(pre_hooks=None, ...)`` unchanged, and Agno treats it as
"feature disabled." That's important so optional-dep envs
(minimal install, headless smoke tests) don't crash on session
construction.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.guardrails.openai import OpenAIModerationGuardrail
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.guardrails.prompt_injection import PromptInjectionGuardrail
from agno.tools.reasoning import ReasoningTools

from ember_code.core.config.settings import Settings

logger = logging.getLogger(__name__)


def create_reasoning_tools(settings: Settings) -> Any | None:
    """Return an Agno ``ReasoningTools`` instance, or ``None`` when
    reasoning is disabled or the module isn't installed."""
    if not settings.reasoning.enabled:
        return None
    try:
        return ReasoningTools(
            add_instructions=settings.reasoning.add_instructions,
            add_few_shot=settings.reasoning.add_few_shot,
        )
    except ImportError:
        logger.debug("agno.tools.reasoning not available")
        return None


def create_guardrails(settings: Settings) -> list | None:
    """Return a list of Agno guardrail ``pre_hooks`` from config, or
    ``None`` when no guardrail flag is enabled.

    Each guardrail is optional — the moderation / PII / prompt-
    injection sub-modules of Agno depend on their own external
    packages (OpenAI SDK, presidio, etc.). Failed imports are
    silent so a partial install still yields a session.
    """
    hooks: list = []
    cfg = settings.guardrails

    if cfg.pii_detection:
        try:
            hooks.append(PIIDetectionGuardrail())
        except ImportError:
            logger.debug("agno.guardrails.pii not available")

    if cfg.prompt_injection:
        try:
            hooks.append(PromptInjectionGuardrail())
        except ImportError:
            logger.debug("agno.guardrails.prompt_injection not available")

    if cfg.moderation:
        try:
            hooks.append(OpenAIModerationGuardrail())
        except ImportError:
            logger.debug("agno.guardrails.openai not available")

    return hooks if hooks else None
