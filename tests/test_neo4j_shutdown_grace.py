"""The shutdown grace has to outlast a checkpoint, or the store is thrown away.

Neo4j checkpoints on a clean shutdown and only then. SIGKILL mid-checkpoint
leaves the per-commit store unusable, so the next session rebuilds it from the
changeset — re-embedding every chunk, which is the entire cost of a load: 16
minutes for celery, 77 for sqlalchemy on an M-series machine.

The old default was ten seconds. Nothing failed loudly; the store was simply
never reusable. Measured on the machine that ran the evaluation: 22 per-commit
state directories, every one of them 16K — a config file and a password, no data
— and the harness re-embedded 18 repositories on every run without anyone
noticing that it need not have.
"""

from __future__ import annotations

import inspect

from ember_code.backend import neo4j_runtime


def test_the_grace_outlasts_a_large_checkpoint():
    """Two minutes is generous for a checkpoint and still bounded."""
    assert neo4j_runtime._DEFAULT_SHUTDOWN_GRACE_SEC >= 60.0, (
        "a grace under a minute cannot checkpoint a large store, and the failure "
        "mode is a graph silently rebuilt from scratch next session"
    )


def test_the_default_is_what_the_runtime_uses():
    signature = inspect.signature(neo4j_runtime.Neo4jRuntime.__init__)
    assert (
        signature.parameters["shutdown_grace_sec"].default
        == neo4j_runtime._DEFAULT_SHUTDOWN_GRACE_SEC
    )


def test_the_kill_warning_states_the_consequence():
    """A warning that says only "SIGKILL" is why this went unnoticed for so long."""
    source = inspect.getsource(neo4j_runtime)
    start = source.find("did not exit within")
    assert start != -1, "the SIGKILL warning moved"
    window = source[start : start + 600]
    for phrase in ("checkpoint", "rebuild", "re-embedding"):
        assert phrase in window, f"the warning does not mention {phrase}"
