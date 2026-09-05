"""Dump the FE-facing wire schema to clients/web/src/protocol/wire-schema.json.

Covers (a) every pydantic protocol message and (b) the ad-hoc dict
payloads returned by RPCs the web client consumes. The web test suite
asserts the field names it reads against this file, so a BE rename
breaks a test instead of silently blanking the UI (the DiffRow /
is_ephemeral / loop_status / p.content bug class).

Regenerate after protocol changes:
    uv run python scripts/dump_wire_schema.py
"""

import inspect
import json
from pathlib import Path

from ember_code.protocol import messages as msg

OUT = Path(__file__).resolve().parents[1] / "clients" / "web" / "src" / "protocol" / "wire-schema.json"

# RPC payloads that are plain dicts/dataclasses built by hand in the
# backend — kept in sync manually with the producing code (file noted).
RPC_PAYLOADS = {
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
    from ember_code.core.pool import AgentInfo

    if hasattr(AgentInfo, "model_fields"):
        return list(AgentInfo.model_fields)
    return [f for f in getattr(AgentInfo, "__dataclass_fields__", {})]


def _scheduled_task_fields() -> list[str]:
    from ember_code.core.scheduler.models import ScheduledTask

    return list(ScheduledTask.model_fields)


def main() -> None:
    schema: dict[str, dict[str, list[str]]] = {"messages": {}, "rpc": {}}

    for _name, cls in inspect.getmembers(msg, inspect.isclass):
        # Anything pydantic that the protocol package re-exports through
        # `messages`. That namespace *is* the FE-facing surface, which
        # is the property worth filtering on.
        #
        # This read `cls.__module__ != msg.__name__` — only classes
        # defined in `messages` itself. Then `messages` became a
        # re-export shim over `protocol/schemas/`, every class's
        # `__module__` became `...schemas.be_events` and friends, and
        # the filter excluded all fifty. The script kept exiting 0 and
        # writing an empty contract, so anybody who ran it destroyed the
        # snapshot — which is why nobody had, and why the FE's
        # wire-contract test spent two months validating against a
        # frozen copy. The guard's input generator failed silently, and
        # a guard checking a fossil passes exactly like one that works.
        if not hasattr(cls, "model_fields"):
            continue
        if not cls.__module__.startswith("ember_code.protocol."):
            continue
        type_field = cls.model_fields.get("type")
        wire_type = getattr(type_field, "default", None) if type_field else None
        key = wire_type if isinstance(wire_type, str) else cls.__name__
        schema["messages"][key] = sorted(cls.model_fields)

    schema["rpc"] = {k: sorted(v) for k, v in RPC_PAYLOADS.items()}
    schema["rpc"]["agent_info"] = sorted(_agent_info_fields())
    schema["rpc"]["scheduled_task"] = sorted(_scheduled_task_fields())

    # Refuse rather than write an empty contract.
    #
    # The failure this script had was silent: it emitted zero messages,
    # exited 0, and printed "0 messages" in a line nobody read. Anybody
    # who ran it replaced a working snapshot with an empty one, so the
    # only reason the contract survived is that nobody ran it for two
    # months. A generator that can destroy its own output has to check
    # its output.
    if len(schema["messages"]) < 20:
        raise SystemExit(
            f"refusing to write: found only {len(schema['messages'])} message types. "
            "The protocol has not shrunk by that much — the more likely explanation is "
            "that the enumeration above stopped matching where the classes live, which "
            "is what happened when `messages` became a re-export shim over "
            "`protocol/schemas/`."
        )

    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(schema['messages'])} messages, {len(schema['rpc'])} rpc payloads)")


if __name__ == "__main__":
    main()
