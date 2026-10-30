"""Agno-agent construction helpers.

Small factory functions that turn ember-code ``Settings`` into
Agno constructor arguments. Extracted from
:mod:`ember_code.core.session.core` so the god-file has fewer
top-level defs and these building blocks can be tested and
imported independently.

Both helpers return ``None`` when the corresponding feature is
switched off in config — the caller passes the ``None`` through to
Agno's ``Agent(reasoning_tools=None, ...)`` / ``Agent(pre_hooks=None,
...)`` unchanged, and Agno treats it as "feature disabled."

They used to claim to degrade the same way when an Agno sub-package
was missing, via four ``except ImportError`` arms. None of them could
ever run: the four classes are imported at module scope below, so an
absent sub-package fails the *import of this module* long before any
handler is reached, and the constructors they wrapped import nothing
that can fail (``PIIDetectionGuardrail`` imports stdlib ``re``;
``PromptInjectionGuardrail`` and ``ReasoningTools`` import nothing;
``OpenAIModerationGuardrail`` defers ``from openai import OpenAI`` to
``check()``, so a missing OpenAI SDK raises at request time, inside
the guardrail, not here). Agno is a hard dependency
(``pyproject.toml``), so there is no partial-install case to degrade
for, and the handlers were only advertising a safety net that was not
attached.
"""

from __future__ import annotations

from typing import Any

from agno.guardrails.openai import OpenAIModerationGuardrail
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.guardrails.prompt_injection import PromptInjectionGuardrail
from agno.tools.reasoning import ReasoningTools

from ember_code.core.config.settings import Settings


def create_reasoning_tools(settings: Settings) -> Any | None:
    """Return an Agno ``ReasoningTools`` instance, or ``None`` when
    reasoning is disabled."""
    if not settings.reasoning.enabled:
        return None
    return ReasoningTools(
        add_instructions=settings.reasoning.add_instructions,
        add_few_shot=settings.reasoning.add_few_shot,
    )


def create_guardrails(settings: Settings) -> list | None:
    """Return a list of Agno guardrail ``pre_hooks`` from config, or
    ``None`` when no guardrail flag is enabled.

    Moderation is the one with an external dependency (the OpenAI SDK),
    and it does not surface here: the guardrail imports the SDK inside
    ``check()``, so an env without it builds a session fine and fails
    the first moderated turn instead.
    """
    hooks: list = []
    cfg = settings.guardrails

    if cfg.pii_detection:
        hooks.append(PIIDetectionGuardrail())

    if cfg.prompt_injection:
        hooks.append(PromptInjectionGuardrail())

    if cfg.moderation:
        hooks.append(OpenAIModerationGuardrail())

    return hooks if hooks else None
