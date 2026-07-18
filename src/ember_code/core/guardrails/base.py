"""Base guardrail class and result model."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from pydantic import BaseModel, Field


class GuardrailResult(BaseModel):
    """Result returned by a guardrail check."""

    passed: bool
    guardrail: str
    message: str
    findings: list[str] = Field(default_factory=list)


class Guardrail:
    """Base class for all guardrails.

    Subclasses must:

    * Override :meth:`check` to inspect the input text and return a
      :class:`GuardrailResult`.
    * Set :attr:`gate_key` to the :class:`GuardrailsConfig` bool field
      that enables this guardrail.  ``__init_subclass__`` enforces this
      at class-definition time so a missing ``gate_key`` fails loudly
      instead of silently registering a no-op guardrail.
    """

    name: str = "base"
    #: Name of the :class:`GuardrailsConfig` attribute that gates this
    #: guardrail.  Empty on the base class (the base is never yielded
    #: by :meth:`iter_enabled`); concrete subclasses must override it.
    gate_key: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Only enforce on *concrete* leaf guardrails -- an intermediate
        # abstract subclass can opt out by leaving gate_key empty and
        # having its own subclasses set it.  We check emptiness at the
        # class attribute level, not via inheritance, so a leaf that
        # forgets to set it fails fast.
        if not cls.__dict__.get("gate_key", "") and not cls.__dict__.get(
            "_abstract_guardrail", False
        ):
            # Walk MRO -- if any base above already set a non-empty
            # gate_key, this subclass inherits it and that's fine.
            inherited = any(base.__dict__.get("gate_key", "") for base in cls.__mro__[1:])
            if not inherited:
                raise TypeError(
                    f"{cls.__name__} must set a non-empty `gate_key` "
                    "class attribute pointing to the GuardrailsConfig "
                    "field that enables it."
                )

    def check(self, text: str) -> GuardrailResult:
        """Check *text* and return a result.  Override in subclasses."""
        return GuardrailResult(
            passed=True,
            guardrail=self.name,
            message="",
            findings=[],
        )

    @classmethod
    def iter_enabled(cls, cfg: object) -> Iterator[Guardrail]:
        """Yield an instance of each subclass whose ``gate_key`` is truthy on *cfg*.

        Walks ``cls.__subclasses__()`` recursively so a deeper hierarchy
        (e.g. ``StrictPIIGuardrail(PIIGuardrail)``) is picked up -- shallow
        ``__subclasses__()`` is a trap.  Concrete subclasses must be
        imported before this is called; ``guardrails/__init__.py`` does
        that eagerly so the registry is populated by import time.
        """
        for sub in cls._all_subclasses():
            key = sub.gate_key
            if key and getattr(cfg, key, False):
                yield sub()

    @classmethod
    def _all_subclasses(cls) -> Iterator[type[Guardrail]]:
        """Yield every subclass of *cls* recursively (depth-first)."""
        for sub in cls.__subclasses__():
            yield sub
            yield from sub._all_subclasses()
