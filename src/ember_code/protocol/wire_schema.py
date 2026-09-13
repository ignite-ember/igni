"""Build the FE-facing wire schema: every message, and the RPC payloads.

The web suite asserts the field names it reads against a snapshot of
this, so a backend rename breaks a test instead of silently blanking
the UI. That only works if the snapshot is regenerated — and it had
not been, for a reason worth recording.

The builder used to live in ``scripts/dump_wire_schema.py`` and
selected classes with ``cls.__module__ != messages.__name__``: "was
this defined in protocol/messages.py?". Then the schemas moved into
``protocol/schemas/*`` and ``messages.py`` became a re-export shim.
Every class's ``__module__`` became ``...schemas.be_events`` and the
like, the filter excluded all of them, and the generator produced a
file with **zero messages** in it. Nobody noticed, because nothing
ran it: the snapshot was maintained by hand from then on, which is
the opposite of what a generated file is for. By the time it was
looked at, the committed copy was missing ``event_seq`` from most
messages.

It lives in the package now so a test can run it and compare —
``tests/test_wire_schema_is_current.py``. A generator nobody runs
rots; one a test runs cannot.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from ember_code.protocol import messages as msg

#: Where the frontend reads it from.
SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "clients"
    / "web"
    / "src"
    / "protocol"
    / "wire-schema.json"
)

#: RPC payloads that are plain dicts built by hand in the backend, so
#: there is no model to reflect on. Kept in step with the producing
#: code by hand — the file that builds each one is named above it,
#: which is the only thing holding these together.
RPC_PAYLOADS: dict[str, list[str]] = {
    # backend/server.py::loop_status
    "loop_status": [
        "active",
        "paused",
        "prompt",
        "iteration_index",
        "iterations_remaining",
        "cap_explicit",
        "announced_total",
    ],
    # backend/server.py::get_pending_messages
    "pending_message": ["role", "content", "received_at", "message_id"],
    # backend/server.py::get_mcp_server_details
    "mcp_server": [
        "name",
        "connected",
        "transport",
        "tool_names",
        "tool_descriptions",
        "resources",
        "prompts",
        "error",
        "policy_blocked",
    ],
}


def _agent_info_fields() -> list[str]:
    from ember_code.core.agents import AgentInfo

    if hasattr(AgentInfo, "model_fields"):
        return list(AgentInfo.model_fields)
    return list(getattr(AgentInfo, "__dataclass_fields__", {}))


def _scheduled_task_fields() -> list[str]:
    from ember_code.core.scheduler.models import ScheduledTask

    return list(ScheduledTask.model_fields)


def build_schema() -> dict[str, dict[str, list[str]]]:
    """Reflect the protocol into ``{"messages": …, "rpc": …}``.

    Membership is "is a :class:`Message`", which is the same rule
    :class:`~ember_code.protocol.registry.MessageRegistry` uses to
    decide what the transports will accept off the wire — the
    definition that actually matters. Abstract bases (``Message``,
    ``RunScopedMessage``) come along under their class names rather
    than a wire type: they carry no ``type`` default, and they
    document the envelope every other entry inherits.
    """
    schema: dict[str, dict[str, list[str]]] = {"messages": {}, "rpc": {}}

    for _name, cls in inspect.getmembers(msg, inspect.isclass):
        if not issubclass(cls, msg.Message) or not hasattr(cls, "model_fields"):
            continue
        type_field = cls.model_fields.get("type")
        wire_type = getattr(type_field, "default", None) if type_field else None
        key = wire_type if isinstance(wire_type, str) else cls.__name__
        schema["messages"][key] = sorted(cls.model_fields)

    schema["rpc"] = {k: sorted(v) for k, v in RPC_PAYLOADS.items()}
    schema["rpc"]["agent_info"] = sorted(_agent_info_fields())
    schema["rpc"]["scheduled_task"] = sorted(_scheduled_task_fields())
    return schema


def render(schema: dict[str, Any] | None = None) -> str:
    """The exact bytes of the snapshot file, for writing or comparing."""
    return json.dumps(schema or build_schema(), indent=2, sort_keys=True) + "\n"


def write(path: Path | None = None) -> tuple[Path, dict[str, dict[str, list[str]]]]:
    schema = build_schema()
    target = path or SNAPSHOT_PATH
    target.write_text(render(schema))
    return target, schema
