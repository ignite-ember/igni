"""GuardrailRunner -- orchestrates enabled guardrails."""

from __future__ import annotations

import logging

from ember_code.core.config.settings import GuardrailsConfig, Settings
from ember_code.core.guardrails.base import Guardrail, GuardrailResult

logger = logging.getLogger(__name__)


class GuardrailRunner:
    """Creates and runs guardrails based on the current :class:`Settings`.

    Membership is data-driven: :meth:`Guardrail.iter_enabled` walks
    ``Guardrail.__subclasses__()`` and yields any concrete guardrail
    whose ``gate_key`` is truthy on ``settings.guardrails``.  Adding
    a new guardrail is therefore a two-step change (define a subclass,
    add a bool to :class:`GuardrailsConfig`) that requires **zero**
    edits here.
    """

    def __init__(self, settings: Settings) -> None:
        cfg = settings.guardrails
        self._guardrails: list[Guardrail] = list(Guardrail.iter_enabled(cfg))
        if not self._guardrails and self._any_flag_enabled(cfg):
            # A cfg flag is on but the registry produced nothing -- most
            # likely the guardrails package was bypassed (someone imported
            # `runner` without going through `guardrails/__init__.py`) so
            # `Guardrail.__subclasses__()` is empty.  Fail-closed guardrails
            # silently disappearing is exactly the kind of bug worth screaming
            # about; keep going but warn loudly.
            logger.warning(
                "GuardrailRunner: config has guardrail flags enabled but the "
                "Guardrail subclass registry is empty -- concrete guardrail "
                "modules were not imported. Ensure `ember_code.core.guardrails` "
                "is imported as a package."
            )

    @staticmethod
    def _any_flag_enabled(cfg: GuardrailsConfig) -> bool:
        """True if any bool field on *cfg* is truthy."""
        return any(bool(v) for v in cfg.model_dump().values())

    @property
    def enabled(self) -> bool:
        """True when at least one guardrail is active."""
        return len(self._guardrails) > 0

    async def check(self, text: str) -> list[GuardrailResult]:
        """Run all enabled guardrails against *text* and return their results.

        Only results that did **not** pass are included in the returned list.
        An empty list means everything passed.

        If a guardrail raises, we log the traceback for observability AND
        append a failing :class:`GuardrailResult` so the caller sees the
        crash as a block (fail-closed).  A silently-swallowed exception on
        a safety check is precisely the wrong default.
        """
        results: list[GuardrailResult] = []
        for guardrail in self._guardrails:
            try:
                result = guardrail.check(text)
            except Exception as exc:
                logger.exception("Guardrail %s raised an error", guardrail.name)
                results.append(
                    GuardrailResult(
                        passed=False,
                        guardrail=guardrail.name,
                        message=f"internal error: {exc!r}",
                        findings=[],
                    )
                )
                continue
            if not result.passed:
                results.append(result)
        return results
