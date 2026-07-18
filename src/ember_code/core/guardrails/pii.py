"""PII detection guardrail using regex patterns."""

from __future__ import annotations

import re
from typing import ClassVar

from ember_code.core.guardrails.base import Guardrail, GuardrailResult

# Patterns map: label -> compiled regex
_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(
        r"(?<!\d)"  # not preceded by digit
        r"(?:\+?1[-.\s]?)?"  # optional country code
        r"(?:\(?\d{3}\)?[-.\s]?)"  # area code
        r"\d{3}[-.\s]?\d{4}"  # subscriber number
        r"(?!\d)"  # not followed by digit
    ),
    "ssn": re.compile(
        r"(?<!\d)"
        r"\d{3}[-\s]?\d{2}[-\s]?\d{4}"
        r"(?!\d)"
    ),
    "credit_card": re.compile(
        r"(?<!\d)"
        r"(?:\d{4}[-\s]?){3}\d{4}"
        r"(?!\d)"
    ),
}


class PIIGuardrail(Guardrail):
    """Detects common PII: emails, phone numbers, SSNs, credit card numbers."""

    name: str = "pii_detection"
    gate_key: ClassVar[str] = "pii_detection"

    def check(self, text: str) -> GuardrailResult:
        findings: list[str] = []
        for label, pattern in _PATTERNS.items():
            matches = pattern.findall(text)
            for match in matches:
                findings.append(f"{label}: {match.strip()}")

        if findings:
            return GuardrailResult(
                passed=False,
                guardrail=self.name,
                message=f"PII detected: {', '.join(findings)}",
                findings=findings,
            )
        return GuardrailResult(
            passed=True,
            guardrail=self.name,
            message="No PII detected.",
            findings=[],
        )
