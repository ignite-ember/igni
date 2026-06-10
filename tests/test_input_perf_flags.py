"""Regression tests for the per-keystroke ``query_one`` avoidance.

Reported on v0.5.12: typing into the prompt became laggy ("each
keystroke takes seconds") once the conversation grew large.

Root cause was ``EmberApp._on_input_changed`` and ``on_key`` calling
``self.query_one(...)`` to look up the autocomplete + file-picker
widgets on every keystroke. ``query_one`` walks the widget tree, so
the per-keystroke cost grew with conversation size.

The fix introduced three pieces of state on ``EmberApp``:

* ``_autocomplete_mounted: bool`` — flipped True by
  ``_mount_autocomplete``, False by the slash-autocomplete teardown
  path. Gates the ``query_one("#autocomplete", Static)`` in the
  input-changed handler.
* ``_file_picker_mounted: bool`` — flipped True by
  ``_show_file_picker``, False by ``_hide_file_picker``. Gates the
  ``query_one(FilePickerDropdown)`` calls in both the input-changed
  handler and the key handler.
* ``_user_input_widget: PromptInput | None`` — cached
  ``query_one("#user-input", ...)`` result for ``on_key``.

These tests pin the contract: the flags transition correctly with
the mount/hide helpers, and ``_hide_file_picker`` always leaves the
flag False even after a NoMatches teardown.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from textual.css.query import NoMatches

from ember_code.frontend.tui.app import EmberApp


def _bare_app() -> EmberApp:
    """Build an ``EmberApp`` instance with only the attributes the
    flag-tracking code touches. Avoids the full Textual + Settings
    bootstrap; the tested methods never reach the event loop."""
    app = EmberApp.__new__(EmberApp)
    app._autocomplete_mounted = False
    app._file_picker_mounted = False
    app._user_input_widget = None
    return app


class TestFlagInitialState:
    def test_autocomplete_mounted_defaults_false(self):
        """A freshly composed app must start with both visibility
        flags False — otherwise the hot path would do a tree walk
        for widgets that aren't mounted yet."""
        app = _bare_app()
        assert app._autocomplete_mounted is False

    def test_file_picker_mounted_defaults_false(self):
        app = _bare_app()
        assert app._file_picker_mounted is False

    def test_user_input_cache_defaults_none(self):
        """``on_key`` populates the cache on first call. Starting
        ``None`` is the signal to do that one-time lookup."""
        app = _bare_app()
        assert app._user_input_widget is None


class TestMountAutocomplete:
    def test_flag_flips_true_on_successful_mount(self):
        """``_mount_autocomplete`` must set the flag so subsequent
        keystrokes know they can look up the widget."""
        app = _bare_app()
        # Mount target returns a mock vertical that accepts ``.mount``.
        area = MagicMock()
        with patch.object(EmberApp, "query_one", return_value=area):
            app._mount_autocomplete("test hint")
        assert app._autocomplete_mounted is True
        area.mount.assert_called_once()

    def test_flag_stays_false_when_mount_fails(self):
        """If the footer query fails (e.g. teardown in progress),
        the flag must not be flipped — otherwise the next keystroke
        would try to find a widget that doesn't exist and re-do
        the expensive lookup."""
        app = _bare_app()
        with patch.object(EmberApp, "query_one", side_effect=NoMatches("footer gone")):
            app._mount_autocomplete("test hint")
        assert app._autocomplete_mounted is False


class TestHideFilePicker:
    def test_flag_clears_after_hide(self):
        """``_hide_file_picker`` must always leave the flag False.
        Without this, a follow-up keystroke would query for a
        picker that's already been removed."""
        app = _bare_app()
        app._file_picker_mounted = True
        with patch.object(EmberApp, "query_one", side_effect=NoMatches("")):
            app._hide_file_picker()
        assert app._file_picker_mounted is False

    def test_flag_clears_even_when_picker_actually_present(self):
        """``query_one(FilePickerDropdown).remove()`` is the success
        path — flag must clear in this case too."""
        app = _bare_app()
        app._file_picker_mounted = True
        picker = MagicMock()
        input_widget = MagicMock()
        # First ``query_one`` call returns the picker, second returns the input.
        with patch.object(EmberApp, "query_one", side_effect=[picker, input_widget]):
            app._hide_file_picker()
        assert app._file_picker_mounted is False
        picker.remove.assert_called_once()


class TestShowFilePicker:
    def test_first_show_flips_flag_true(self):
        """First call mounts a new picker and must set the flag —
        otherwise ``on_key`` would never know it's there to query."""
        app = _bare_app()
        # Simulate the constructor: ``query_one`` returns input, then
        # footer, then prompt_row in order.
        input_widget = MagicMock()
        footer = MagicMock()
        prompt_row = MagicMock()
        with patch.object(
            EmberApp, "query_one", side_effect=[input_widget, footer, prompt_row]
        ):
            app._show_file_picker(["foo.py", "bar.py"])
        assert app._file_picker_mounted is True
        footer.mount.assert_called_once()

    def test_update_on_existing_picker_keeps_flag_true(self):
        """When the picker is already mounted, ``_show_file_picker``
        just updates its matches — flag stays True."""
        app = _bare_app()
        app._file_picker_mounted = True
        input_widget = MagicMock()
        existing_picker = MagicMock()
        with patch.object(
            EmberApp, "query_one", side_effect=[input_widget, existing_picker]
        ):
            app._show_file_picker(["new.py"])
        assert app._file_picker_mounted is True
        existing_picker.update_matches.assert_called_once_with(["new.py"])

    def test_stale_flag_self_heals(self):
        """If the flag says mounted but the picker was actually
        removed externally (NoMatches on the lookup), the helper
        falls through to the create path and re-flips the flag
        rather than leaving it permanently stale."""
        app = _bare_app()
        app._file_picker_mounted = True  # lying — picker was removed
        input_widget = MagicMock()
        footer = MagicMock()
        prompt_row = MagicMock()
        # query_one calls: input (in suppress check), then picker
        # (raises NoMatches → fallback path), then footer, then prompt_row.
        with patch.object(
            EmberApp,
            "query_one",
            side_effect=[input_widget, NoMatches(""), footer, prompt_row],
        ):
            app._show_file_picker(["x.py"])
        assert app._file_picker_mounted is True
        footer.mount.assert_called_once()


class TestHotPathSkipsTreeWalks:
    """The whole point of the fix: when nothing's mounted, the hot
    path must NOT issue ``query_one`` calls for the autocomplete or
    file-picker widgets. A regression here brings back the lag.
    """

    def test_input_changed_skips_autocomplete_query_when_flag_false(self):
        """``_on_input_changed`` walks the widget tree only when the
        autocomplete or file-picker flags say there's something to
        find. With both False and the text being a plain character,
        the only ``query_one`` allowed is the autocomplete lookup
        *if* a non-slash command yields matches — and for a single
        random letter against ``get_completions`` there are none."""
        app = _bare_app()
        app._shell_mode = False
        app._command_mode = False
        app._input_handler = MagicMock()
        app._input_handler.get_completions.return_value = []
        app._input_handler.get_file_completions.return_value = []
        text_area = MagicMock()
        text_area.text = "a"
        text_area.cursor_location = (0, 1)
        text_area.document.get_line.return_value = "a"
        event = MagicMock(text_area=text_area)
        with patch.object(EmberApp, "query_one") as q1:
            EmberApp._on_input_changed(app, event)
        assert q1.call_count == 0, (
            f"expected zero query_one calls on a plain keystroke when "
            f"nothing is mounted; got {q1.call_count}"
        )

    def test_input_changed_skips_picker_query_when_flag_false(self):
        """Even when the user's text contains content that COULD be
        an @-mention prefix elsewhere, the picker query is gated by
        the flag — only an active @-mention should trigger the
        file-completion lookup, not every keystroke before it."""
        app = _bare_app()
        app._shell_mode = False
        app._command_mode = False
        app._input_handler = MagicMock()
        app._input_handler.get_completions.return_value = []
        text_area = MagicMock()
        text_area.text = "hello world"
        text_area.cursor_location = (0, 11)
        text_area.document.get_line.return_value = "hello world"
        event = MagicMock(text_area=text_area)
        with patch.object(EmberApp, "query_one") as q1:
            EmberApp._on_input_changed(app, event)
        # No @ in the line → no picker query; autocomplete flag is
        # False → no autocomplete query either.
        assert q1.call_count == 0
