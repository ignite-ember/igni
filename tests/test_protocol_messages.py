"""Tests for protocol message serialization — validates the BE↔FE contract.

Covers all message types, round-trip serialization, and the Agno event serializer.
"""

from unittest.mock import MagicMock

import pytest
from agno.run.agent import RunContentEvent, RunStartedEvent

from ember_code.protocol import messages as msg
from ember_code.protocol.serializer import serialize_event
from ember_code.transport.unix_socket import deserialize_message


class TestAllMessageTypes:
    """Every message type should instantiate, serialize, and deserialize."""

    @pytest.mark.parametrize(
        "cls",
        [
            msg.ContentDelta,
            msg.ToolStarted,
            msg.ToolCompleted,
            msg.ToolError,
            msg.ModelCompleted,
            msg.RunStarted,
            msg.RunCompleted,
            msg.RunError,
            msg.ReasoningStarted,
            msg.HITLRequest,
            msg.TaskCreated,
            msg.TaskUpdated,
            msg.TaskIteration,
            msg.TaskStateUpdated,
            msg.CommandResult,
            msg.StatusUpdate,
            msg.SessionListResult,
            msg.SessionCleared,
            msg.Info,
            msg.Error,
            msg.SchedulerEvent,
            msg.RunPaused,
            msg.UserMessage,
            msg.QueueMessage,
            msg.HITLResponse,
            msg.Command,
            msg.Cancel,
            msg.SessionSwitch,
            msg.SessionList,
            msg.ModelSwitch,
            msg.MCPToggle,
            msg.Shutdown,
        ],
    )
    def test_instantiate_default(self, cls):
        """Every message type should instantiate with defaults."""
        instance = cls()
        assert instance.type != ""

    @pytest.mark.parametrize(
        "cls",
        [
            msg.ContentDelta,
            msg.ToolStarted,
            msg.ToolCompleted,
            msg.ToolError,
            msg.ModelCompleted,
            msg.RunStarted,
            msg.RunCompleted,
            msg.RunError,
            msg.Info,
            msg.Error,
            msg.UserMessage,
            msg.Command,
            msg.Cancel,
            msg.Shutdown,
        ],
    )
    def test_json_roundtrip(self, cls):
        """Serialize to JSON and back should preserve data."""
        original = cls()
        json_str = original.model_dump_json()
        restored = cls.model_validate_json(json_str)
        assert restored.type == original.type

    def test_content_delta_fields(self):
        m = msg.ContentDelta(text="hello", is_thinking=True)
        assert m.text == "hello"
        assert m.is_thinking is True
        assert m.type == "content_delta"

    def test_tool_completed_with_diff(self):
        rows = [("+ 1  line", "#69db7c on #003d00"), ("  2  ctx", "")]
        m = msg.ToolCompleted(summary="Edited", has_markup=True, diff_rows=rows)
        assert m.diff_rows == rows
        assert m.has_markup is True

    def test_hitl_request_with_args(self):
        m = msg.HITLRequest(
            requirement_id="r1",
            tool_name="run_shell",
            tool_args={"args": ["git", "push"]},
        )
        assert m.tool_args["args"] == ["git", "push"]

    def test_run_paused_with_requirements(self):
        req = msg.HITLRequest(requirement_id="r1", tool_name="save_file")
        m = msg.RunPaused(run_id="run-1", requirements=[req])
        assert len(m.requirements) == 1
        assert m.requirements[0].requirement_id == "r1"

    def test_status_update_fields(self):
        m = msg.StatusUpdate(
            model="gpt-4",
            cloud_connected=True,
            cloud_org="My Org",
            context_tokens=5000,
            max_context=128000,
        )
        assert m.model == "gpt-4"
        assert m.cloud_connected is True


class TestUnixSocketDeserialization:
    """Test the deserializer used by Unix socket transport."""

    def test_all_be_to_fe_types(self):
        types = [
            msg.ContentDelta,
            msg.ToolStarted,
            msg.ToolCompleted,
            msg.ToolError,
            msg.ModelCompleted,
            msg.RunStarted,
            msg.RunCompleted,
            msg.RunError,
            msg.ReasoningStarted,
            msg.Info,
            msg.Error,
            msg.CommandResult,
            msg.StatusUpdate,
            msg.SessionListResult,
            msg.SessionCleared,
        ]
        for cls in types:
            original = cls()
            json_line = original.model_dump_json()
            restored = deserialize_message(json_line)
            assert restored is not None, f"Failed to deserialize {cls.__name__}"
            assert restored.type == original.type

    def test_all_fe_to_be_types(self):
        types = [
            msg.UserMessage,
            msg.QueueMessage,
            msg.HITLResponse,
            msg.Command,
            msg.Cancel,
            msg.SessionSwitch,
            msg.SessionList,
            msg.ModelSwitch,
            msg.MCPToggle,
            msg.Shutdown,
        ]
        for cls in types:
            original = cls()
            json_line = original.model_dump_json()
            restored = deserialize_message(json_line)
            assert restored is not None, f"Failed to deserialize {cls.__name__}"


class TestAgnoEventSerializer:
    """Test the Agno event → protocol message serializer."""

    def test_content_event(self):
        event = RunContentEvent(content="hello world")
        proto = serialize_event(event)
        assert isinstance(proto, msg.ContentDelta)
        assert proto.text == "hello world"
        assert proto.is_thinking is False

    def test_empty_content_returns_none(self):
        event = RunContentEvent(content="")
        proto = serialize_event(event)
        assert proto is None

    def test_run_started_event(self):
        event = RunStartedEvent(agent_name="editor", run_id="r1", model="gpt-4")
        proto = serialize_event(event)
        assert isinstance(proto, msg.RunStarted)
        assert proto.agent_name == "editor"
        assert proto.run_id == "r1"

    def test_unknown_event_returns_none(self):
        class FakeEvent:
            pass

        proto = serialize_event(FakeEvent())
        assert proto is None

    def test_fallback_content_event(self):
        """Events with string .content attribute should be treated as content."""
        event = MagicMock(spec=[])
        event.content = "fallback text"
        proto = serialize_event(event)
        assert isinstance(proto, msg.ContentDelta)
        assert proto.text == "fallback text"
