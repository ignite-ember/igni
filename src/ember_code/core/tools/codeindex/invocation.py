"""Shared invocation wrapper for the two agent-facing tool methods.

Both ``codeindex_query`` and ``codeindex_tree`` used to carry the same
scaffolding: capture ``t0``, await the service, serialize the result,
append a telemetry entry, and wrap raised exceptions as an
:class:`ErrorResponse`. Duplicating that six-line block twice in
:class:`CodeIndexTools` was the audit's Pattern 6 (duplicated wrapper).

:class:`ToolInvocationRecorder` collapses those two blocks into one
async method. The recorder owns the timing → serialize → record →
error-wrap flow, so each tool method now reads as three lines:
build typed input, hand to recorder, return the recorder's string.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable
from typing import Any

from pydantic import BaseModel

from ember_code.core.tools.codeindex.schemas import ErrorResponse, TelemetryEntry
from ember_code.core.tools.codeindex.serializer import JsonSerializer
from ember_code.core.tools.codeindex.telemetry import TelemetryLog

logger = logging.getLogger(__name__)


class ToolInvocationRecorder:
    """Timing + serialization + telemetry wrapper for tool method calls.

    Constructed once per toolkit with the shared serializer and
    telemetry log; each :meth:`invoke` call runs one tool invocation
    end-to-end.
    """

    def __init__(self, *, serializer: JsonSerializer, telemetry: TelemetryLog) -> None:
        self._serializer = serializer
        self._telemetry = telemetry

    async def invoke(
        self,
        *,
        tool_name: str,
        telemetry_args: dict[str, Any],
        coro: Awaitable[BaseModel],
    ) -> str:
        """Run one tool invocation and return its JSON string.

        Times the coroutine, serializes the result, records a
        :class:`TelemetryEntry`, and returns the rendered string. When
        ``coro`` raises, logs the stack via ``logger.exception`` and
        returns a serialized :class:`ErrorResponse`.

        The ``except Exception`` here is a deliberate top-level safety
        net at the agent boundary: the tool contract is "always
        returns a JSON string", so a service raising is mapped to an
        error response the agent can read. Tests rely on this
        conversion (``tests/test_codeindex_tools.py::test_internal_exception_surfaces_error``).
        """
        t0 = time.monotonic()
        try:
            result = await coro
            response = self._serializer.dumps(result)
            self._telemetry.record(
                TelemetryEntry(
                    ts=time.time(),
                    tool=tool_name,
                    duration_ms=round((time.monotonic() - t0) * 1000, 1),
                    args=telemetry_args,
                    response=response,
                    response_chars=len(response),
                )
            )
            return response
        except Exception as exc:
            logger.exception("%s failed", tool_name)
            return self._serializer.dumps(ErrorResponse(error=f"{tool_name} failed: {exc}"))
