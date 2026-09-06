"""``/ctx`` raised on every invocation, and three tests said it worked.

``ContextBreakdownView`` annotates its one field with
``ContextBreakdown``, imported under ``TYPE_CHECKING``. With
``from __future__ import annotations`` that annotation is a *string*
Pydantic resolves when the class is built, and the name did not exist
at runtime — so the model never finished building and every
construction raised::

    PydanticUserError: `ContextBreakdownView` is not fully defined

The user saw ``session routing failed: `ContextBreakdownView` is not
fully defined …`` with a link to the Pydantic docs. Not a degraded
answer — a stack-trace fragment in a chat bubble.

**How it survived.** Everything that touched ``/ctx`` asserted on
something the failure also satisfied:

* the e2e spec matched ``/context|token|floor/i`` against the
  transcript, and the error message contains "Context";
* ``rpc_sweep.py`` never calls it — ``/ctx`` is a slash command, not
  an RPC, so the sweep's 104 rows do not reach it at all;
* the unit tests around it call ``to_command_result`` on an instance
  built by ``model_construct`` or on a hand-rolled stub, both of which
  skip validation and therefore skip the thing that was broken.

So the rule here is: **build the model the way the product builds it**
— ``from_domain``, full validation — and check the rendered text, not
that a call returned.
"""

from __future__ import annotations

import pytest

from ember_code.backend.schemas_context import ContextBreakdownView
from ember_code.core.session.schemas import ContextBreakdown


class TestTheModelIsFullyDefined:
    def test_pydantic_finished_building_it(self):
        """The state that was false in production.

        ``__pydantic_complete__`` is exactly the flag whose being
        ``False`` produced the user-visible error, so it is worth
        asserting directly and not only through behaviour.
        """
        assert ContextBreakdownView.__pydantic_complete__ is True

    def test_from_domain_does_not_raise(self):
        view = ContextBreakdownView.from_domain(
            ContextBreakdown(total=1000, runs=400, floor=600)
        )

        assert view.breakdown.total == 1000

    @pytest.mark.parametrize(
        "first",
        [
            "ember_code.backend.schemas_context",
            "ember_code.core.session",
            "ember_code.backend.command_handler",
        ],
    )
    def test_it_is_complete_whichever_module_is_imported_first(self, first: str):
        """Order-independence, in a subprocess, because the fix is about
        import order and this process has already imported everything.

        ``ContextBreakdown`` cannot be imported into
        ``schemas_context`` at runtime **at all**: it drags in
        ``core.session.__init__``, which reaches
        ``interactive → interactive_loop → commands →
        backend.command_handler``, which is what imports
        ``schemas_context``. Putting the import at the top killed one
        entry point; moving it to the foot killed a different one —
        ``command_handler`` first, which is how the test suite loads.
        Hence ``Any`` on the field.

        Three entry points because two of them each passed while the
        third was broken.
        """
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import {first};"
                "from ember_code.backend.schemas_context import ContextBreakdownView as V;"
                "print(V.__pydantic_complete__)",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"


class TestTheCardReads:
    """The numbers, not just the absence of an exception.

    A view that renders is not a view that renders *correctly*, and the
    percentage is computed here rather than passed in — the one place
    this class can be wrong without raising.
    """

    def test_the_percentage_is_runs_over_total(self):
        text = ContextBreakdownView.from_domain(
            ContextBreakdown(total=1000, runs=400, floor=600)
        ).to_command_result().content

        assert "40.0% of total" in text

    def test_the_three_numbers_are_all_present_and_grouped(self):
        text = ContextBreakdownView.from_domain(
            ContextBreakdown(total=1_234_567, runs=234_567, floor=1_000_000)
        ).to_command_result().content

        assert "1,234,567 tokens" in text
        assert "234,567 tokens" in text
        assert "1,000,000 tokens" in text

    def test_an_empty_context_does_not_divide_by_zero(self):
        """``total`` is 0 on a session that has not run anything, which
        is the state the app is in the moment it opens — the most
        likely moment for a user to press ``/ctx`` out of curiosity."""
        text = ContextBreakdownView.from_domain(
            ContextBreakdown(total=0, runs=0, floor=0)
        ).to_command_result().content

        assert "0.0% of total" in text

    def test_it_does_not_render_a_pydantic_error(self):
        """The regression as the user met it.

        Stated against the rendered text rather than the exception,
        because what reached them was a chat bubble, and a later
        refactor that swallows the raise and renders the message would
        pass every test above.
        """
        text = ContextBreakdownView.from_domain(
            ContextBreakdown(total=10, runs=5, floor=5)
        ).to_command_result().content

        assert "not fully defined" not in text
        assert "errors.pydantic.dev" not in text
        assert text.startswith("**Context breakdown**")


class TestTheDomainModelStaysRenderFree:
    """The reason the view exists at all.

    ``core.session.schemas`` must not import ``CommandResult``; that
    separation is what the module docstring promises, and it is one
    grep to keep honest.
    """

    def test_the_domain_model_has_no_render_method(self):
        assert not hasattr(ContextBreakdown, "to_command_result")


@pytest.mark.parametrize("total,runs", [(100, 0), (100, 100), (1, 1)])
def test_the_percentage_stays_within_bounds(total: int, runs: int):
    text = ContextBreakdownView.from_domain(
        ContextBreakdown(total=total, runs=runs, floor=total - runs)
    ).to_command_result().content

    percent = float(text.split("% of total")[0].split("(")[-1])
    assert 0.0 <= percent <= 100.0
