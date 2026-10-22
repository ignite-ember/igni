"""What ``apply_drift`` refuses to write.

This method appends a version. Everything it lets through becomes a
document somebody reads, so the checks in front of the write are the
whole safety of it.

The case that motivated these: agno does not raise when a provider
fails. It returns normally, sets ``status`` to ERROR, and puts the
error message into ``content``. Code that reads ``content`` as the
answer will store "Token Plan usage limit reached: Upgrade your Token
Plan or purchase Credits for more usage. (2056)" as the new version of
the document — and then re-pin the edge, reporting it as resolved.
Found by the drift eval, which graded three such messages as failing
documents before it learned to tell the difference.
"""

from __future__ import annotations

from types import SimpleNamespace

from ember_code.backend.server_artifacts import _run_failed, _strip_fence


class TestSpottingAFailedRun:
    def test_an_error_status_is_a_failure_however_full_the_content(self):
        response = SimpleNamespace(
            status=SimpleNamespace(value="ERROR"),
            content="Token Plan usage limit reached: Upgrade your Token Plan. (2056)",
        )
        assert _run_failed(response) is True

    def test_a_plain_string_status_is_read_too(self):
        assert _run_failed(SimpleNamespace(status="ERROR", content="x")) is True

    def test_case_does_not_decide_it(self):
        assert _run_failed(SimpleNamespace(status="error", content="x")) is True

    def test_a_completed_run_is_not_a_failure(self):
        response = SimpleNamespace(status=SimpleNamespace(value="COMPLETED"), content="# Doc")
        assert _run_failed(response) is False

    def test_a_response_with_no_status_at_all_is_not_a_failure(self):
        # Older shapes, and every hand-built stub in the tests.
        assert _run_failed(SimpleNamespace(content="# Doc")) is False


class TestStrippingAFence:
    """Asked for "the document and nothing else", models still fence."""

    def test_a_fenced_document_loses_its_fence(self):
        assert _strip_fence("```markdown\n# Title\n\nBody\n```") == "# Title\n\nBody"

    def test_an_unfenced_document_is_untouched(self):
        assert _strip_fence("# Title\n\nBody") == "# Title\n\nBody"

    def test_a_fence_inside_the_document_survives(self):
        # A document that legitimately contains a code block must not
        # be gutted by the stripper.
        doc = "# Title\n\n```py\nx = 1\n```\n\nTail."
        assert _strip_fence(doc) == doc

    def test_an_opening_fence_with_no_close_is_left_alone(self):
        # Truncated output. Better to keep it and let the emptiness or
        # the reader catch it than to silently drop the first line.
        assert _strip_fence("```\n# Title").startswith("```")
