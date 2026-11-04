"""What a warm base's igni imports before it says it is ready.

``docs/CLOUD.md`` §9. A warm base snapshot is taken once igni writes
``ready``, and every producer's VM is a restore of it: whatever igni has
imported by then is in every VM already, in memory pages the restores
share. Whatever it imports later, each producer's first request pays
for, and holds a private copy of.

Most of a bound backend is imported with ``ember_code.backend.app``.
:attr:`WarmImports.MODULES` is the rest: what the first bind, the first
request and the first run import on top, as measured by
``scripts/measure_warm_base.py`` (bind to a PostgreSQL database, answer a
request, run a turn against a model). Run it again when the boot path
changes; it prints what is still imported after ``ready``.

A module that is not installed is skipped: an optional dependency must
not stop igni from serving.
"""

from __future__ import annotations

import importlib
import logging
import time
from typing import ClassVar

logger = logging.getLogger(__name__)


class WarmImports:
    """Imports what a bound backend's first run would, before ``ready``."""

    MODULES: ClassVar[tuple[str, ...]] = (
        # Opening the producer's database: the driver, and the schema
        # check every open makes (alembic, which brings mako and pygments).
        "psycopg",
        "sqlalchemy.engine.strategies",
        "sqlalchemy.ext.compiler",
        "alembic.command",
        "alembic.runtime.migration",
        "alembic.script",
        "mako.template",
        "ember_code.core.db.migrations",
        # A model call: the OpenAI client's request and streaming path,
        # and the async backend under httpx.
        "openai.resources.chat.completions.completions",
        "openai.resources.chat.chat",
        "openai.lib.streaming.chat",
        "openai.pagination",
        "anyio._backends._asyncio",
        "anyio.streams.stapled",
        "anyio.streams.tls",
        # An agent run, and the tool hooks every tool call passes.
        "agno.agent._hooks",
        "agno.agent._response",
        "agno.agent._telemetry",
        "agno.utils.callables",
        "agno.utils.json_schema",
        "ember_code.core.hooks.bypass_guard",
        "ember_code.core.hooks.bypass_explainer",
        "ember_code.backend.workflow_runner",
        # The standard library's share of the above.
        "concurrent.futures.thread",
        "ctypes",
        "tomllib",
    )

    def __init__(self, modules: tuple[str, ...] | None = None) -> None:
        self.modules = self.MODULES if modules is None else modules
        self.skipped: list[str] = []

    def run(self) -> None:
        started = time.perf_counter()
        for name in self.modules:
            try:
                importlib.import_module(name)
            except ImportError as exc:
                self.skipped.append(name)
                logger.debug("warm import of %s skipped: %s", name, exc)
        logger.info(
            "warmed %d modules in %.0f ms",
            len(self.modules) - len(self.skipped),
            (time.perf_counter() - started) * 1000,
        )


__all__ = ["WarmImports"]
