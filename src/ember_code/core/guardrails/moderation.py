"""Moderation guardrail stub (placeholder for OpenAI moderation API)."""

from __future__ import annotations

from typing import ClassVar

from ember_code.core.guardrails.base import Guardrail, GuardrailResult


class ModerationGuardrail(Guardrail):
    """Placeholder moderation guardrail.

    Always passes through.  Replace with an actual call to the
    OpenAI Moderation API (or similar) when ready -- the ``async``
    protocol is already in place so an ``await httpx.post(...)`` slots
    in without another signature change.
    """

    name: str = "moderation"
    gate_key: ClassVar[str] = "moderation"

    def check(self, text: str) -> GuardrailResult:
        return GuardrailResult(
            passed=True,
            guardrail=self.name,
            message="Moderation check passed (stub).",
            findings=[],
        )
