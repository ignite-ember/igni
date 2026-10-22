"""A Neo4j we adopted has to answer the same liveness question.

``Neo4jRuntime`` spawns Neo4j processes, but it can also *adopt* one
another backend already started — that is what keeps two BEs on the
same project from fighting over a data directory. An adopted process
is represented by ``_ExternalProcessHandle``, which has a PID and a
signalable process group but no ``Popen``.

It also had no ``returncode``, and three liveness checks read exactly
that. So adopting a process instead of spawning one raised
``AttributeError`` inside ``start_for_artifacts``; the caller caught
it, logged "neo4j attach failed; continuing without it", and the
session came up with artifacts **disabled**.

That failure is the reason this test exists rather than a unit test
of a property getter. Nothing was down: Neo4j was running and
reachable, the documents were all there, and every artifact RPC
answered "Artifacts are disabled" — including the Cypher panel, which
reported an empty graph. Found by opening the app on a project whose
store had been seeded a minute earlier by another process.
"""

from __future__ import annotations

import os

from ember_code.backend.neo4j_runtime import _ExternalProcessHandle


class TestItReportsLiveness:
    def test_a_running_process_reports_none(self):
        """``None`` is what ``Popen`` means by "still running", and
        every call site tests ``returncode is None``."""
        handle = _ExternalProcessHandle(os.getpid())
        assert handle.returncode is None

    def test_a_dead_process_reports_an_exit_code(self):
        # A PID that cannot exist. The real code cannot recover an
        # exit status for a process it did not spawn, so any non-None
        # value is correct; what matters is that it is not None.
        handle = _ExternalProcessHandle(2**22)
        assert handle.returncode is not None

    def test_it_is_a_property_not_a_method(self):
        """The call sites read ``state.proc.returncode`` with no
        parentheses. A method here would be truthy always, so every
        adopted process would look alive forever — including one that
        had crashed."""
        assert not callable(_ExternalProcessHandle(os.getpid()).returncode)


class TestItSatisfiesTheCheckThatBroke:
    def test_the_liveness_expression_runs_without_raising(self):
        """The literal expression from ``start_for_artifacts``.

        Written as the call site writes it, because the bug was not a
        wrong answer — it was an ``AttributeError`` from asking at
        all, and a test that called the property by a friendlier name
        would not have caught it.
        """

        class _State:
            def __init__(self, proc):
                self.proc = proc

        state = _State(_ExternalProcessHandle(os.getpid()))
        assert state is not None and state.proc.returncode is None

    def test_the_shape_matches_what_popen_offers(self):
        """The handle claims to be ``Popen``-shaped; the claim is what
        the runtime relies on when it duck-types."""
        handle = _ExternalProcessHandle(os.getpid())
        for name in ("pid", "returncode", "send_signal", "terminate", "kill"):
            assert hasattr(handle, name), f"missing {name}"
