"""Regression tests for the model-switch / status-bar race.

Reported on v0.5.14: user runs ``/model``, picks ``MiniMax-M2.7``;
the chat shows ``Switched to model: MiniMax-M2.7`` but the footer
status bar continues to display ``MiniMax-M3``. Root cause was
``BackendClient.switch_model`` being fire-and-forget — the FE's
follow-up ``update_status_bar`` called ``get_status`` BEFORE the
RPC landed, reading the stale model.

Two flows must both update the bar correctly:

1. **Picker path** — ``ModelPickerWidget.Selected`` → handler
   calls ``await backend.switch_model`` then
   ``update_status_bar``. The await is what was missing before.
2. **Slash command path** — ``/model <name>`` direct. Runs
   entirely on the BE inline; the FE only sees the
   ``CommandResult``. New ``action="model_switched"`` tells the
   FE to refresh the bar.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ember_code.protocol import messages as msg


class TestPickerPathAwaitsRPC:
    """``BackendClient.switch_model`` must await the BE confirmation
    rather than fire-and-forget. Without the await the picker
    handler's follow-up ``update_status_bar`` reads pre-switch
    state."""

    @pytest.mark.asyncio
    async def test_switch_model_awaits_backend(self):
        """A test double records the send order — the switch RPC
        must resolve BEFORE the function returns."""
        from ember_code.frontend.tui.backend_client import BackendClient

        client = BackendClient.__new__(BackendClient)
        events: list[str] = []

        async def fake_send_and_wait(message):
            assert isinstance(message, msg.ModelSwitch)
            events.append("be-acked")
            return msg.Info(text="Switched to gpt-7")

        client._send_and_wait = fake_send_and_wait  # type: ignore[method-assign]

        events.append("call-start")
        result = await client.switch_model("gpt-7")
        events.append("call-end")

        # The BE ack MUST land between call-start and call-end —
        # if the wrapper returned early (fire-and-forget) we'd see
        # ``be-acked`` last (or not at all this turn).
        assert events == ["call-start", "be-acked", "call-end"]
        assert isinstance(result, msg.Info)


class TestSlashCommandTriggersRefresh:
    """``/model <name>`` direct switch sets ``action='model_switched'``
    so the FE knows to refresh the status bar — the slash-command
    path doesn't otherwise touch the bar."""

    @pytest.mark.asyncio
    async def test_direct_switch_sets_model_switched_action(self):
        """Drive the command handler directly with a known model
        name and assert the result action."""
        from unittest.mock import MagicMock

        from ember_code.backend.command_handler import CommandHandler

        handler = CommandHandler.__new__(CommandHandler)
        session = MagicMock()
        session.settings.models.registry = {"MiniMax-M2.7": {}}
        session.settings.models.default = "MiniMax-M3"
        session._build_main_agent = MagicMock(return_value="rebuilt-team")
        handler._session = session

        result = await handler._cmd_model("MiniMax-M2.7")

        from ember_code.protocol.messages import CommandAction

        assert result.action == CommandAction.MODEL_SWITCHED
        assert "MiniMax-M2.7" in result.content
        # Side effect: settings default flipped and the team was
        # rebuilt with the new model.
        assert session.settings.models.default == "MiniMax-M2.7"
        session._build_main_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_model_does_not_emit_action(self):
        """Negative case: ``/model <bogus>`` returns an error and
        no ``model_switched`` action — the FE must not refresh
        the bar against a model that didn't actually swap."""
        from ember_code.backend.command_handler import CommandHandler

        handler = CommandHandler.__new__(CommandHandler)
        session = MagicMock()
        session.settings.models.registry = {"MiniMax-M2.7": {}}
        handler._session = session

        result = await handler._cmd_model("does-not-exist")

        from ember_code.protocol.messages import CommandAction

        assert result.kind == "error"
        assert result.action != CommandAction.MODEL_SWITCHED
