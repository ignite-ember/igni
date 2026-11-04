"""The warm base's objects made immortal, so using them writes nothing.

``docs/CLOUD_RESUME_TODO.md`` item 2. Every producer's VM is a restore of
one warm base, and a page the producer's igni writes to becomes its own:
stored when it suspends, read back when it resumes cold. CPython writes
to an object every time it takes or drops a reference to it — calling a
function, reading a constant, looking up a class — so merely *running*
igni's code turned some 25 MB of the base's pages into each producer's
own while it bound, 40 MB after a turn (measured in a Linux container,
``scripts/measure_resume.py``; diagnostic, not the image's numbers).

Since 3.12 CPython has immortal objects (PEP 683): ``None``, small ints,
interned strings. Their reference count is a fixed value that taking or
dropping a reference leaves alone — no write — and they are never freed.
:class:`BaseObjects` gives every object alive when the warm base is
taken the same reference count as ``None``: modules, classes, functions,
code, constants, the strings and tuples they hold. They would have lived
as long as the process anyway; what they hold may still change (a dict
still takes new keys), only the count stops being written.

Two more writes the first use of a base object makes are made now, in
the base, while it is walked: every string's hash, which CPython
computes on first use and keeps in the string, and every class's version
tag, which it assigns on the first attribute lookup through the class's
cache. Measured, these were a quarter of the base pages written with
one or two words changed.

Applied once, in the warm base, just before ``ready`` and after the
collector has frozen what is there (:meth:`CloudGuest.listening`). Only
on a 64-bit CPython whose immortal count is the one this module knows how
to check; anywhere else it does nothing.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import platform
import sys
import time

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Immortalised(BaseModel):
    objects: int
    #: Strings whose hash was computed now, classes given a version tag.
    hashed: int = 0
    typed: int = 0
    ms: float


class BaseObjects:
    """Makes every object alive now immortal (CPython 3.12+, 64-bit)."""

    @staticmethod
    def immortal_refcount() -> int | None:
        """``None``'s reference count — this interpreter's immortal value — or
        ``None`` where objects are not immortal that way."""
        if (
            platform.python_implementation() != "CPython"
            or sys.version_info < (3, 12)
            or sys.maxsize <= 2**32
        ):
            return None
        value = ctypes.c_ssize_t.from_address(id(None)).value
        # Immortal on 64-bit: the low 32 bits, read as signed, are negative.
        return value if ctypes.c_int32(value).value < 0 else None

    @classmethod
    def immortalise(cls) -> Immortalised | None:
        refcount = cls.immortal_refcount()
        if refcount is None:
            return None
        started = time.perf_counter()
        stack: list[object] = list(gc.get_objects())
        stack.extend(sys.modules.values())
        # The walk's own list is not the base's.
        own = id(stack)
        count = hashed = typed = 0
        while stack:
            obj = stack.pop()
            key = id(obj)
            cell = ctypes.c_ssize_t.from_address(key)
            # Immortal already — made so by this walk, or by CPython — is
            # seen: no set of every object visited (it would stay in the
            # base as freed memory). An object CPython made immortal is not
            # walked into, and holds nothing of igni's.
            if cell.value == refcount or key == own:
                continue
            cell.value = refcount
            count += 1
            # ``type()``, never ``isinstance``: that asks a lazy proxy for
            # its ``__class__``, which imports what it stands for.
            kind = type(obj)
            if kind is str or kind is bytes:
                hash(obj)
                hashed += 1
            elif issubclass(kind, type):
                cls._version_tag(obj)  # type: ignore[arg-type]
                typed += 1
            stack.extend(gc.get_referents(obj))
        result = Immortalised(
            objects=count,
            hashed=hashed,
            typed=typed,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        logger.info(
            "warm base: %d objects made immortal, %d strings hashed, %d classes tagged, in %.0f ms",
            result.objects,
            result.hashed,
            result.typed,
            result.ms,
        )
        return result

    #: A name no class has: looking it up goes through the class's
    #: attribute cache, which gives the class its version tag.
    _PROBE = "_igni_warm_base_version_probe"

    @classmethod
    def _version_tag(cls, klass: type) -> None:
        try:
            type.__getattribute__(klass, cls._PROBE)
        except AttributeError:
            pass
        except Exception:
            # A class whose lookup does more than look (a metaclass's
            # ``__getattr__`` misbehaving): left as it is.
            logger.debug("no version tag for %r", klass, exc_info=True)


__all__ = ["BaseObjects", "Immortalised"]
