"""Agno's own telemetry, which igni keeps off.

Agno posts an event to ``https://os-api.agno.com`` for every agent, team
and workflow run, and its default is on: ``agno.api.settings`` names the
host, ``agno.api.agent`` / ``team`` / ``workflow`` post to it, and each
``Agent`` / ``Team`` / ``Workflow`` re-reads ``AGNO_TELEMETRY`` at every run
(``agno.agent._init.set_telemetry`` and its siblings), overriding what the
constructor said. What it sends is metadata, not content — run, session
and agent ids, the provider and model id, the database class — but it is
an outbound call to a third party that none of the compliance documents
name, from a product sold as self-hosted.

Three places keep it off:

* ``ember_code/__init__.py`` applies :meth:`AgnoTelemetry.apply_process_default`
  before anything imports Agno, so every process that imports igni — the
  CLI, the desktop and cloud backends, igni-cloud, the decisions service —
  starts with ``AGNO_TELEMETRY=false`` unless it was set explicitly.
* Every ``Agent`` / ``Team`` igni builds passes :attr:`CONSTRUCTOR_VALUE`
  (``telemetry=False``), so a process whose environment lost the variable
  still sends nothing. The environment wins over the constructor in Agno,
  so this is the floor, not the switch.
* The processes igni starts with an environment of their own (extension
  services, whose MCP stdio launch inherits almost nothing) are handed the
  variable by :meth:`child_environment`.

**An explicit ``AGNO_TELEMETRY=true``.** Outside air-gapped mode it is
honoured: somebody who sets it has asked Agno to be told, and nothing else
in igni's deployment story forbids a customer sending what they choose.
Under air-gapped mode (``air_gapped: true`` or ``IGNI_AIR_GAPPED=1``) it is
forced back to ``false`` with a warning — a run cannot reach
``os-api.agno.com`` from inside the network, and a setting that only
produces failed connections is one the deployment did not mean.
:meth:`enforce` makes that decision once settings are loaded;
:meth:`apply_process_default` makes it at import for a process whose
air-gap comes from the environment alone.

Agno's evals (``AccuracyEval``, ``ReliabilityEval``, ...) and ``AgentOS``
do **not** read the variable: their ``telemetry`` field defaults to
``True`` and is all that gates them. igni passes ``telemetry=False`` to
every eval it builds (``core/evals/assertion_runner.py``) and never builds
an ``AgentOS``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from typing import ClassVar

from pydantic import BaseModel

from ember_code.core.air_gap import air_gapped_by_environment

logger = logging.getLogger(__name__)


class AgnoTelemetryDecision(BaseModel):
    """What :meth:`AgnoTelemetry.enforce` did to the environment."""

    #: ``AGNO_TELEMETRY`` after the decision.
    value: str
    #: Whether an explicit ``true`` was overruled because igni is air-gapped.
    overruled: bool = False


class AgnoTelemetry:
    """Keeps Agno's run telemetry to ``os-api.agno.com`` switched off."""

    #: The variable Agno reads at every run.
    ENV: ClassVar[str] = "AGNO_TELEMETRY"
    #: The value that switches it off.
    OFF: ClassVar[str] = "false"
    #: Passed as ``telemetry=`` to every ``Agent`` and ``Team`` igni builds.
    CONSTRUCTOR_VALUE: ClassVar[bool] = False

    @classmethod
    def apply_process_default(cls, environ: MutableMapping[str, str] | None = None) -> None:
        """Off unless set; forced off when the environment says air-gapped."""
        env = os.environ if environ is None else environ
        env.setdefault(cls.ENV, cls.OFF)
        cls.enforce(env, air_gapped=air_gapped_by_environment(env))

    @classmethod
    def enforce(
        cls, environ: MutableMapping[str, str], *, air_gapped: bool
    ) -> AgnoTelemetryDecision:
        """Apply the air-gap rule to ``environ`` (see the module docstring).

        Only a real ``True`` counts as air-gapped, as in
        :func:`~ember_code.core.air_gap.export_to`.
        """
        environ.setdefault(cls.ENV, cls.OFF)
        if air_gapped is True and cls.is_on(environ):
            logger.warning(
                "%s=%s is ignored: igni is air-gapped, so Agno's run telemetry to "
                "os-api.agno.com stays off.",
                cls.ENV,
                environ[cls.ENV],
            )
            environ[cls.ENV] = cls.OFF
            return AgnoTelemetryDecision(value=cls.OFF, overruled=True)
        return AgnoTelemetryDecision(value=environ[cls.ENV])

    @classmethod
    def is_on(cls, environ: Mapping[str, str] | None = None) -> bool:
        """Agno's own reading of the variable: on only for ``true``."""
        env = os.environ if environ is None else environ
        return env.get(cls.ENV, "").strip().lower() == "true"

    @classmethod
    def child_environment(cls) -> dict[str, str]:
        """The variable for a process igni starts with an environment of its own.

        The process's own decision, already enforced: ``false`` unless an
        explicit opt-in survived :meth:`enforce`.
        """
        return {cls.ENV: os.environ.get(cls.ENV, cls.OFF)}


__all__ = ["AgnoTelemetry", "AgnoTelemetryDecision"]
