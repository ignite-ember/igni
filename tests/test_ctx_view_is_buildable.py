"""`/ctx` has to be able to build its own view model.

``ContextBreakdownView`` declares ``breakdown: ContextBreakdown``, but
the type is imported under ``TYPE_CHECKING`` and the module uses
``from __future__ import annotations`` — so the annotation stayed a
string and pydantic never had the class. Every attempt to construct the
view raised:

    `ContextBreakdownView` is not fully defined; you should define
    `ContextBreakdown`, then call `ContextBreakdownView.model_rebuild()`

Nothing caught it because the module *imports* cleanly. The failure only
happens when someone asks for a context breakdown, which is to say: in
front of a user, at the moment they type `/ctx`.
"""

from __future__ import annotations

from ember_code.backend.schemas_context import ContextBreakdownView
from ember_code.core.session.schemas import ContextBreakdown


def _breakdown() -> ContextBreakdown:
    return ContextBreakdown(total=120_000, runs=45_000, floor=12_000)


def test_the_view_can_be_constructed():
    """The regression itself: this raised PydanticUserError."""
    view = ContextBreakdownView.from_domain(_breakdown())

    assert view.breakdown.total == 120_000


def test_it_renders_a_command_result():
    """Constructing it is half the job — `/ctx` renders it, so the
    template has to survive the round trip too."""
    result = ContextBreakdownView.from_domain(_breakdown()).to_command_result()

    text = getattr(result, "content", "") or ""
    assert "Context breakdown" in text
    assert "120" in text.replace(",", "")


def test_the_model_reports_itself_as_complete():
    """Pydantic's own answer, rather than an inference from a
    successful call — a later refactor that reintroduces the lazy
    annotation fails here first."""
    assert ContextBreakdownView.__pydantic_complete__ is True
