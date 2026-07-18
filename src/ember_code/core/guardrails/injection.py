"""Prompt-injection detection guardrail."""

from __future__ import annotations

import re
from typing import ClassVar

from ember_code.core.guardrails.base import Guardrail, GuardrailResult

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_previous", re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE)),
    ("system_prompt", re.compile(r"system\s*prompt\s*:", re.IGNORECASE)),
    ("you_are_now", re.compile(r"you\s+are\s+now", re.IGNORECASE)),
    ("disregard", re.compile(r"disregard\s+(all\s+)?(prior|previous|above)\s+", re.IGNORECASE)),
    ("new_instructions", re.compile(r"new\s+instructions?\s*:", re.IGNORECASE)),
    (
        "override",
        re.compile(r"override\s+(your\s+)?(instructions|rules|guidelines)", re.IGNORECASE),
    ),
    ("pretend", re.compile(r"pretend\s+(you\s+are|to\s+be)\s+", re.IGNORECASE)),
    ("jailbreak", re.compile(r"(jailbreak|DAN\s*mode|developer\s*mode)", re.IGNORECASE)),
    (
        "reveal_prompt",
        re.compile(r"(reveal|show|print|output)\s+(your\s+)?(system\s+)?prompt", re.IGNORECASE),
    ),
    ("act_as", re.compile(r"act\s+as\s+(if|a|an|the)\s+", re.IGNORECASE)),
]


class PromptInjectionGuardrail(Guardrail):
    """Detects common prompt-injection patterns via regex."""

    name: str = "prompt_injection"
    gate_key: ClassVar[str] = "prompt_injection"

    def check(self, text: str) -> GuardrailResult:
        findings: list[str] = []
        for label, pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                findings.append(label)

        if findings:
            return GuardrailResult(
                passed=False,
                guardrail=self.name,
                message=f"Potential prompt injection detected: {', '.join(findings)}",
                findings=findings,
            )
        return GuardrailResult(
            passed=True,
            guardrail=self.name,
            message="No injection patterns detected.",
            findings=[],
        )
