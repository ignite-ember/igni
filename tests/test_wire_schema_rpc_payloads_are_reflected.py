"""The non-envelope RPC payloads in the wire schema come from the models.

``wire_schema.RPC_PAYLOADS`` used to be three hand-written field lists,
justified by a comment saying the producers were "plain dicts built by
hand in the backend, so there is no model to reflect on". All three
producers return Pydantic models:

* ``BackendServer.loop_status`` -> ``LoopStatusSnapshot``
* ``BackendServer.get_pending_messages`` -> ``list[PendingMessage]``
* ``BackendServer.get_mcp_server_details`` -> ``list[MCPServerSnapshot]``

and the hand-kept copy had already dropped a field: ``mcp_server`` was
missing ``tools_disabled``. The snapshot exists so a backend rename
breaks a test instead of blanking the UI; a field the snapshot never
knew about is a field that protection never covered.

These tests assert the schema is derived from the models, so the next
field added to any of the three cannot go missing.
"""

from __future__ import annotations

from ember_code.backend.schemas_context import PendingMessage
from ember_code.backend.schemas_loop import LoopStatusSnapshot
from ember_code.backend.schemas_mcp import MCPServerSnapshot
from ember_code.protocol.wire_schema import build_schema

_MODELS = {
    "loop_status": LoopStatusSnapshot,
    "pending_message": PendingMessage,
    "mcp_server": MCPServerSnapshot,
}


def test_each_rpc_payload_matches_its_model_exactly():
    """Not "is a subset of" — exactly. A stale entry is as wrong as a
    missing one; the frontend contract test compares both directions."""
    rpc = build_schema()["rpc"]

    for key, model in _MODELS.items():
        assert rpc[key] == sorted(model.model_fields), (
            f"wire schema entry {key!r} has drifted from "
            f"{model.__name__}: schema={rpc[key]}, "
            f"model={sorted(model.model_fields)}"
        )


def test_mcp_server_carries_tools_disabled():
    """The field the hand-written list had actually lost.

    ``tools_disabled`` is what the MCP panel reads to grey out a
    per-tool toggle; it shipped on the model and never reached the
    snapshot, so the contract test could not have caught a rename of
    it. Pinned by name because "the list is reflected now" is the
    general claim and this is the specific regression.
    """
    assert "tools_disabled" in build_schema()["rpc"]["mcp_server"]
