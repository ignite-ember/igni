"""Tests for tui/status_tracker.py — context tracking and status bar delegation."""

from unittest.mock import MagicMock

from ember_code.frontend.tui.status_tracker import StatusTracker


def _make_tracker(bar: MagicMock | None = None) -> StatusTracker:
    """Create a StatusTracker with a mocked app and optional bar."""
    app = MagicMock()
    tracker = StatusTracker(app)
    # Patch _bar to return our mock (or None)
    tracker._bar = MagicMock(return_value=bar)
    return tracker


class TestInitialState:
    def test_defaults(self):
        app = MagicMock()
        tracker = StatusTracker(app)
        assert tracker._context_tokens == 0
        assert tracker.max_context_tokens == 128_000


class TestStartEndRun:
    def test_start_run_delegates(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.start_run()
        bar.start_run.assert_called_once()

    def test_end_run_delegates(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.end_run()
        bar.end_run.assert_called_once()

    def test_start_run_no_bar(self):
        tracker = _make_tracker(bar=None)
        tracker.start_run()  # should not raise

    def test_end_run_no_bar(self):
        tracker = _make_tracker(bar=None)
        tracker.end_run()  # should not raise


class TestUpdateStatusBar:
    def test_delegates_model_and_cloud(self):
        from ember_code.protocol.messages import StatusUpdate

        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        backend = MagicMock()
        backend.get_status.return_value = StatusUpdate(
            model="test-model", cloud_connected=True, cloud_org="My Org"
        )
        tracker._app._backend = backend
        tracker.update_status_bar()
        bar.update_model.assert_called_once_with("test-model")
        bar.set_cloud_status.assert_called_once_with(True, "My Org")

    def test_no_backend(self):
        tracker = _make_tracker(bar=MagicMock())
        if hasattr(tracker._app, "_backend"):
            del tracker._app._backend
        tracker.update_status_bar()  # should not raise

    def test_cloud_org_empty(self):
        from ember_code.protocol.messages import StatusUpdate

        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        backend = MagicMock()
        backend.get_status.return_value = StatusUpdate(
            model="test-model", cloud_connected=False, cloud_org=""
        )
        tracker._app._backend = backend
        tracker.update_status_bar()
        bar.set_cloud_status.assert_called_once_with(False, "")


class TestContextTokens:
    """The context cache is now a backend-driven count, refreshed by
    ``RunController._post_run_compaction`` via
    ``BackendClient.count_context_tokens`` (Agno's per-model
    tokenizer). The tracker just caches the result and pipes it to
    the bar."""

    def test_set_context_tokens_replaces(self):
        tracker = _make_tracker()
        tracker.set_context_tokens(5000)
        assert tracker._context_tokens == 5000
        tracker.set_context_tokens(8000)
        assert tracker._context_tokens == 8000

    def test_set_context_tokens_floors_at_zero(self):
        tracker = _make_tracker()
        tracker.set_context_tokens(-50)
        assert tracker._context_tokens == 0

    def test_update_context_usage_delegates(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.max_context_tokens = 100_000
        tracker.set_context_tokens(50_000)
        tracker.update_context_usage()
        bar.set_context_usage.assert_called_once_with(50_000, 100_000)

    def test_update_context_usage_with_zero(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.update_context_usage()
        bar.set_context_usage.assert_called_once_with(0, tracker.max_context_tokens)

    def test_update_context_usage_no_bar(self):
        tracker = _make_tracker(bar=None)
        tracker.set_context_tokens(5000)
        tracker.update_context_usage()  # should not raise


class TestSetIdeStatus:
    def test_delegates(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.set_ide_status("VS Code", True)
        bar.set_ide_status.assert_called_once_with("VS Code", True)

    def test_no_bar(self):
        tracker = _make_tracker(bar=None)
        tracker.set_ide_status("VS Code", True)  # should not raise


class TestSetCloudStatus:
    def test_delegates(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.set_cloud_status(True, "my-org")
        bar.set_cloud_status.assert_called_once_with(True, "my-org")

    def test_default_org_name(self):
        bar = MagicMock()
        tracker = _make_tracker(bar=bar)
        tracker.set_cloud_status(False)
        bar.set_cloud_status.assert_called_once_with(False, "")


class TestReset:
    def test_clears_state(self):
        tracker = _make_tracker()
        tracker.set_context_tokens(3000)
        tracker.reset()
        assert tracker._context_tokens == 0
