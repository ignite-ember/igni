"""One place that decides whether the Neo4j knowledge runtime comes up.

There used to be two switches. ``knowledge.enabled`` in config —
default ``True``, honoured by nine call sites (the knowledge manager,
the session constructor, ``/knowledge``, the tips, the ops layer) — and
``IGNI_NEO4J_RUNTIME``, an environment variable checked in exactly one
place, which silently overrode all nine. Config said the feature was
on, every message the user could read said it was on, and it was off.

Worse, the env var could not be set by the people it gated: a
Finder-launched ``.app`` does not inherit a shell environment, so the
panel's advice to "set ``IGNI_NEO4J_RUNTIME=1`` for the backend" named
a switch with no reachable handle. The feature was not opt-in; it was
unreachable.

So config decides, and the env var survives only as a deliberate
override for the two cases where a *process* — not a user — needs to
force the answer: CI pinning a run, and an escape hatch for someone
whose sidecar will not start and who needs the backend up regardless.
Being an override, it works in both directions; the old code only
understood "set means on", which left no way to say no.
"""

from __future__ import annotations

import os
from typing import Any

#: Values that mean "off" when the override is set. Anything else that
#: is set at all means "on" — matching the old ``if os.environ.get``
#: behaviour for ``=1``, which is what existing setups pass.
_FALSEY = {"0", "false", "no", "off", ""}

ENV_OVERRIDE = "IGNI_NEO4J_RUNTIME"


def knowledge_runtime_enabled(settings: Any) -> bool:
    """Whether to build and attach the Neo4j knowledge runtime.

    ``settings.knowledge.enabled`` is the answer, unless
    :data:`ENV_OVERRIDE` is set — in which case it wins, either way.

    Defensive about the settings shape (``getattr`` with a default)
    because this is called from the boot path, where a config that
    fails to produce a knowledge section should cost the user their
    knowledge base, not their backend.
    """
    override = os.environ.get(ENV_OVERRIDE)
    if override is not None:
        return override.strip().lower() not in _FALSEY
    knowledge = getattr(settings, "knowledge", None)
    return bool(getattr(knowledge, "enabled", False))
