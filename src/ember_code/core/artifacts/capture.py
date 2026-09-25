"""Retrieval capture — what the agent read, so the write can cite it.

The problem this solves: an agent that writes a document after reading
three others will reliably cite one of them. The other two derivations
are real and invisible, and nothing downstream can tell that the
document went stale when they changed.

:class:`RetrievalLedger` closes that gap without asking the model for
anything. Every search records the exact ``(artifact_id, n)`` it
returned; the next write attaches whatever it did not explicitly cite
as ``candidate`` edges. Candidates are visible and reviewable but do
**not** drive staleness — the asymmetry is deliberate. Capture is
generous because missing a derivation is unrecoverable, and
confirmation is strict because a false dependency that raises alarms
teaches people to ignore alarms.
"""

from __future__ import annotations

from collections import OrderedDict

from ember_code.core.artifacts.models import EdgeState, VersionPin


class RetrievalLedger:
    """Per-session record of which artifact versions were read.

    Bounded by ``capacity`` on an LRU, because a long session touches
    far more than any one document is plausibly built on, and an
    unbounded ledger turns the oldest read of the day into a
    dependency of the last thing written.
    """

    DEFAULT_CAPACITY: int = 32

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        self._reads: OrderedDict[str, VersionPin] = OrderedDict()

    def record(self, pins: list[VersionPin]) -> None:
        """Note that these versions were read.

        A re-read moves the pin to the front *and* refreshes ``n``:
        the relevant fact is the version the agent saw most recently,
        since that is the text it is actually working from.
        """
        for pin in pins:
            self._reads.pop(pin.artifact_id, None)
            self._reads[pin.artifact_id] = pin.model_copy(update={"state": EdgeState.CANDIDATE})
            while len(self._reads) > self._capacity:
                self._reads.popitem(last=False)

    def candidates(self, *, exclude: list[str] | None = None) -> list[VersionPin]:
        """Reads not already accounted for by an explicit citation.

        ``exclude`` carries the declared sources and the artifact
        being written — an artifact must never derive from itself,
        and a revision reads its own previous version constantly.
        """
        skip = set(exclude or [])
        return [pin for aid, pin in self._reads.items() if aid not in skip]

    def forget(self, artifact_id: str) -> None:
        self._reads.pop(artifact_id, None)

    def clear(self) -> None:
        self._reads.clear()

    def __len__(self) -> int:
        return len(self._reads)
