"""Fresh randomness for a process restored from a shared snapshot.

``docs/CLOUD.md`` §9. Every producer's VM is a restore of one warm base,
so every igni starts with the same memory — including the state of each
pseudo-random generator seeded at import. The guest agent's
``post_restore`` reseeds the kernel, which covers ``os.urandom``,
``secrets``, ``uuid.uuid4`` and ``random.SystemRandom``; what lives in
Python's own memory it cannot reach. :class:`ProcessEntropy` reseeds
that from the kernel: at binding — the first moment a VM is one
producer's — and after every restore igni notices.

What it reseeds:

* the ``random`` module's generator, which libraries use for retry
  jitter and sampling;
* every other ``random.Random`` held at module level (found by looking,
  so a library added later is covered); ``SystemRandom`` reads the
  kernel each time and is left alone;
* ``tempfile``'s name sequence, which reseeds itself only when the
  process id changes — and a restored process keeps its id.

Not reachable: ``PYTHONHASHSEED``'s string-hash salt is fixed at
interpreter start, so restored VMs share it. It only matters for hash
flooding, which igni's inputs (one producer's own requests) do not
expose; the image may set ``PYTHONHASHSEED`` per boot, which a warm base
cannot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys
import tempfile
import types

from ember_code.core.activity import background

logger = logging.getLogger(__name__)


class ProcessEntropy:
    """Reseeds the process's pseudo-random state from the kernel."""

    #: After a restore, the full look for module-level generators waits this
    #: long, so it is not on the path of the first requests (see :meth:`after_restore`).
    RESCAN_AFTER_SECONDS = 2.0

    def __init__(self) -> None:
        #: Module-level generators found by the last full look.
        self._known: list[random.Random] = []

    def reseed(self) -> int:
        """Look for every generator and reseed it; returns how many. At binding."""
        self._known = self._module_generators()
        return self._reseed(self._known)

    def reseed_known(self) -> int:
        """Reseed what the last look found, without looking again."""
        return self._reseed(self._known)

    @staticmethod
    def _reseed(generators: list[random.Random]) -> int:
        random.seed()
        for generator in generators:
            generator.seed()
        # Rebuilt, with a fresh generator, on the next temporary name.
        tempfile._name_sequence = None  # type: ignore[attr-defined]
        count = 1 + len(generators)
        logger.debug("reseeded %d generators", count)
        return count

    async def after_restore(self, _restored: object) -> None:
        """Reseed at once what is known; look again for new ones a moment later.

        A restored VM's memory comes back lazily, page by page as it is
        touched; walking every module's namespace touches thousands of
        pages on the path of the first request after a resume. What the
        last look found is reseeded at once, the random module and
        tempfile included; a generator a module made since is found by
        the look that follows, off that path.
        """
        self.reseed_known()
        with contextlib.suppress(RuntimeError):
            background.daemon(self._rescan_later(), name="entropy-rescan")

    async def _rescan_later(self) -> None:
        await asyncio.sleep(self.RESCAN_AFTER_SECONDS)
        known = {id(g) for g in self._known}
        found = self._module_generators()
        self._reseed([g for g in found if id(g) not in known])
        self._known = found

    @staticmethod
    def _module_generators() -> list[random.Random]:
        found: dict[int, random.Random] = {}
        for module in list(sys.modules.values()):
            # Plain modules only: a lazy module (``importlib.util.LazyLoader``
            # and its kind) imports itself when its namespace is touched,
            # and reseeding must not import anything.
            if type(module) is not types.ModuleType:
                continue
            namespace = module.__dict__
            for value in list(namespace.values()):
                # ``type()``, not ``isinstance``: the latter asks a lazy
                # proxy for its ``__class__``, which imports what it stands for.
                kind = type(value)
                if (
                    issubclass(kind, random.Random)
                    and not issubclass(kind, random.SystemRandom)
                    and value is not getattr(random, "_inst", None)
                ):
                    found[id(value)] = value
        return list(found.values())


__all__ = ["ProcessEntropy"]
