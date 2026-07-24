"""Protocol message types for BE↔FE communication.

**This module is now a re-export shim.** The wire schemas live in
:mod:`ember_code.protocol.schemas`, split by concern:

* :mod:`.schemas.envelope` — base :class:`Message` +
  :class:`RunHeader` + :class:`RunScopedMessage` mixin.
* :mod:`.schemas.enums` — every wire-contract StrEnum.
* :mod:`.schemas.be_events` — BE → FE run / tool / task events.
* :mod:`.schemas.mirroring` — multi-client broadcasts.
* :mod:`.schemas.fe_actions` — FE → BE actions.
* :mod:`.schemas.rpc` — process-split RPC.
* :mod:`.schemas.push` — push notifications + typed channel enum.
* :mod:`.schemas.internal` — intermediate value objects (not wire).

Existing callsites (``from ember_code.protocol.messages import
UserMessage`` etc.) keep working unchanged — every public symbol
is re-exported below. New code should import directly from the
sub-modules under :mod:`ember_code.protocol.schemas` when the
concern is clear (e.g. a slash-command handler importing from
:mod:`.schemas.be_events`).

The star-import is load-bearing:
:class:`ember_code.protocol.registry.MessageRegistry` reflection-
scans this module's namespace for :class:`Message` subclasses at
construction time, so every wire message must land here for the
deserializer to discover it. ``schemas.__all__`` is the source of
truth for what's exported.
"""

from __future__ import annotations

from ember_code.protocol.schemas import *  # noqa: F401,F403
from ember_code.protocol.schemas import __all__ as _schemas_all

__all__ = list(_schemas_all)
