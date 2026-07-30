"""Session compaction sub-package.

Public entry point for the three collaborators that own the
auto + manual compaction lifecycle:

* :class:`CompactionCoordinator` — orchestrates the compaction
  passes, fires PreCompact / PostCompact hooks, delegates to
  the two collaborators below.
* :class:`FallbackSummariser` — free-text summariser that
  runs when Agno's structured summariser returns empty
  (MiniMax-M2.7 workaround).
* :class:`ContextBreakdownReporter` — owns token accounting
  for the ``/ctx`` slash command.

Consumers reach for :class:`CompactionCoordinator`; the other
two are exported for isolated testing.
"""

from ember_code.core.session.compaction.context_breakdown_reporter import (
    ContextBreakdownReporter,
)
from ember_code.core.session.compaction.coordinator import CompactionCoordinator
from ember_code.core.session.compaction.fallback_summariser import (
    FallbackSummariser,
    TranscriptMessage,
)

__all__ = [
    "CompactionCoordinator",
    "ContextBreakdownReporter",
    "FallbackSummariser",
    "TranscriptMessage",
]
