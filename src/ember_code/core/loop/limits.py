"""Shared ``/loop`` iteration-count limits.

Both the slash command (``/loop <prompt>``) and the agent tool
(:py:meth:`LoopTools.loop_start`) reference the same defaults
and hard ceiling — kept here so the two paths can't drift.

Semantics:

* ``LOOP_DEFAULT_MAX_ITERATIONS`` — the *batch size* an implicit-cap
  loop runs before auto-extending. The user invoked ``/loop`` with
  no leading number, so they expressed no expectation about how
  many iterations to run; this is just the safety-net batch
  before we re-check whether to keep going.
* ``LOOP_HARD_CAP`` — the absolute ceiling. Even implicit loops
  terminate at this many iterations regardless of remaining work.
  Catches a model that's stuck in a useless loop.
"""

from __future__ import annotations

LOOP_DEFAULT_MAX_ITERATIONS = 30
LOOP_HARD_CAP = 200
