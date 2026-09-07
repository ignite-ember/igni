"""The desktop app's whole interface to its backend, built in CI.

`RpcRouter.build_table()` merges every handler's `@rpc`-tagged methods
into one dispatch table and calls `validate_rpc_table`, which raises if
an `RpcMethod` has no handler. That is a good guard against the "added
an enum member, forgot to register it" mistake.

**It had never run in CI.** `build_table` is called from
`session_orchestrator`, `app.py` and `__main__` — all production paths.
So adding an enum value without a handler left the suite green and the
desktop app dead on launch: R-8's shape, in the one component a user
meets first.

Nothing was actually wrong. The table builds, covers all 104 members,
and rejects duplicates. This file is that check, moved to where it can
fail before a release rather than during one.

Beyond exhaustiveness, two properties `validate_rpc_table` cannot see:

* **every binding takes the dispatch convention.** The table is
  `{method: fn(args: dict)}`, and a decorated method with a different
  signature builds fine and fails at call time — for one method, in the
  app, with no test naming it. Forty-nine of the hundred handlers are
  referenced nowhere in this suite, so a signature check over all of
  them is worth more than a test for any one;
* **the pool-level guards refuse.** Four RPCs are intercepted by
  `SessionOrchestrator` before per-runtime routing, and are registered
  in the table as deliberate "you should never reach here" traps. A trap
  that returned `None` instead of raising would make a mis-route
  invisible, which is the failure it exists to make loud.
"""

from __future__ import annotations

import inspect
import pathlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ember_code.backend.rpc_router import RpcRouter
from ember_code.protocol.rpc import RpcMethod


def _router() -> RpcRouter:
    """A router with the collaborators stubbed.

    The handlers are constructed but not called, so the stubs only have
    to satisfy construction — `project_dir` is the one real value any of
    them reads, for the file-completion index.
    """
    backend = MagicMock()
    backend.project_dir = Path(tempfile.mkdtemp())
    return RpcRouter(backend=backend, transport=MagicMock(), login=MagicMock(), push=MagicMock())


class TestTheTableBuilds:
    def test_it_builds_at_all(self):
        """Which is the part that only happened in production before."""
        table = _router().build_table()

        assert len(table) >= 100, len(table)

    def test_every_declared_method_has_a_binding(self):
        """`validate_rpc_table` raises inside `build_table`, so this
        passes by not raising — and states the property, so a reader
        does not have to know that."""
        table = _router().build_table()
        declared = {m.value for m in RpcMethod}
        bound = {str(k) for k in table}

        assert declared - bound == set(), sorted(declared - bound)
        assert len(declared) == len(bound) == len(table)

    def test_the_enum_has_not_quietly_emptied(self):
        """A comparison of two derived sets is happy when both are
        empty. This is the floor that stops that."""
        assert len(list(RpcMethod)) >= 100, len(list(RpcMethod))


class TestEveryBindingCanBeCalled:
    def test_each_takes_a_single_args_dict(self):
        """The dispatch convention. A decorated method with a different
        signature builds into the table fine and fails at call time — in
        the app, for one method, with no test naming it."""
        table = _router().build_table()
        wrong: dict[str, str] = {}

        for method, fn in table.items():
            positional = [
                p
                for p in inspect.signature(fn).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.name != "self"
            ]
            if len(positional) != 1:
                wrong[str(method)] = str(inspect.signature(fn))

        assert not wrong, (
            f"these bindings do not take exactly one positional argument, so dispatch will "
            f"fail when the app calls them: {wrong}"
        )

    def test_the_dispatcher_awaits_the_async_ones(self):
        """Both kinds are allowed, and that is the property worth
        pinning rather than the count.

        This test first asserted no handler was async, on the assumption
        that the table is called synchronously. Ten are — and the
        dispatcher handles it: `result = handler(args)` followed by
        `if asyncio.iscoroutine(result) or asyncio.isfuture(result)`.
        The assumption was wrong and the code was right.

        What would be a real bug is that check disappearing while async
        handlers remain. Then the RPC returns a coroutine, `_serialize`
        makes something meaningless of it, and the app sees a *success*
        containing nothing — worse than an error, because nothing looks
        wrong.
        """
        table = _router().build_table()
        coroutines = sorted(str(m) for m, fn in table.items() if inspect.iscoroutinefunction(fn))

        assert coroutines, (
            "no handler is async any more. If that is deliberate the await below is dead "
            "code, but the likelier reading is that this test has stopped finding them."
        )

        dispatcher = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src/ember_code/backend/message_dispatcher.py"
        ).read_text()

        assert (
            "asyncio.iscoroutine(result)" in dispatcher and "result = await result" in dispatcher
        ), (
            f"{len(coroutines)} handlers are async and the dispatcher no longer awaits the "
            f"result. Each of them now answers with an un-awaited coroutine, which "
            f"serialises to nonsense and reports success: {coroutines[:3]}…"
        )


class TestThePoolLevelGuardsRefuse:
    """Four RPCs are intercepted before per-runtime routing and
    registered here as traps. A trap that does nothing is not a trap."""

    def test_all_four_are_in_the_table(self):
        table = _router().build_table()

        assert RpcRouter.POOL_LEVEL_RPCS, "no pool-level RPCs declared"
        for method in RpcRouter.POOL_LEVEL_RPCS:
            assert method in table, f"{method} is intercepted but not registered as a guard"

    @pytest.mark.parametrize("method", sorted(RpcRouter.POOL_LEVEL_RPCS, key=str), ids=str)
    def test_reaching_one_raises(self, method):
        table = _router().build_table()

        with pytest.raises(RuntimeError, match="pool-level RPC dispatched"):
            table[method]({})


class TestDuplicateRegistrationIsRejected:
    def test_two_handlers_claiming_one_method_is_an_error(self):
        """`build_table` raises on a duplicate key, which is what makes
        the handler list's order "descriptive, not load-bearing". Shown
        rather than trusted, because it is the property that lets
        somebody reorder that list safely.
        """
        router = _router()
        first = router._handlers[0]
        router._handlers = [first, first]

        with pytest.raises(RuntimeError, match="duplicate RPC binding"):
            router.build_table()
