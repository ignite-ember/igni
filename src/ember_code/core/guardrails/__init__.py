"""Guardrails enforcement for igni.

Eager-import contract
---------------------
:class:`GuardrailRunner` builds its list of active guardrails by walking
``Guardrail.__subclasses__()`` and picking the ones whose ``gate_key``
is truthy on the current :class:`GuardrailsConfig`.  Python only
populates ``__subclasses__()`` with classes that have actually been
*imported*, so every concrete ``Guardrail`` subclass MUST be imported
here at package-init time.  Do NOT convert these to lazy imports --
that would silently empty the subclass registry and make every
guardrail a no-op.
"""

from ember_code.core.guardrails.base import Guardrail, GuardrailResult
from ember_code.core.guardrails.injection import PromptInjectionGuardrail
from ember_code.core.guardrails.moderation import ModerationGuardrail
from ember_code.core.guardrails.pii import PIIGuardrail
from ember_code.core.guardrails.runner import GuardrailRunner

__all__ = [
    "Guardrail",
    "GuardrailResult",
    "GuardrailRunner",
    "ModerationGuardrail",
    "PIIGuardrail",
    "PromptInjectionGuardrail",
]
