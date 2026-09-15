"""The counted facts must survive every layer between the JSONL and the graph.

There are four: the wire op, ``UpsertItemOp.to_item``, ``CodeIndexItemCreate``,
and the codec's node params. Each one enumerates fields explicitly, and the codec
reads with ``getattr(item, name, 0)`` — so a field missing from any layer does
not raise. It writes 0. A graph then answers "which classes are too big" with
nothing, which is indistinguishable from a repository of small classes.

That happened twice while these fields were being added: once because
``to_item`` builds its payload field-by-field rather than from ``model_dump``,
and once because ``CodeIndexItemCreate`` had never declared them. Hence a test
that walks the whole chain rather than any single hop.
"""

from __future__ import annotations

import pytest

from ember_code.core.code_index.delta.ops import UpsertItemOp
from ember_code.core.code_index.neo4j_codec import Neo4jRowCodec

COUNTED = {
    "fan_in": 7,
    "fan_out": 3,
    "test_refs": 2,
    "importer_count": 11,
    "member_count": 19,
    "error_handlers": 5,
    "empty_handlers": 4,
    "broad_handlers": 1,
}


def _op(**overrides) -> UpsertItemOp:
    payload = {
        "op": "upsert_item",
        "id": "item-1",
        "type": "entity",
        "name": "Big",
        "path": "src/a.py::Big",
        "content": "a class",
        **COUNTED,
        "swallow_lines": [12, 40],
    }
    payload.update(overrides)
    return UpsertItemOp.model_validate(payload)


class TestTheWholeChain:
    def test_the_wire_op_parses_them(self):
        op = _op()
        for field, value in COUNTED.items():
            assert getattr(op, field) == value, field
        assert op.swallow_lines == [12, 40]

    def test_to_item_carries_them(self):
        """The regression: fields on the op alone are dropped here."""
        item = _op().to_item()
        for field, value in COUNTED.items():
            assert getattr(item, field, None) == value, f"{field} lost in to_item"
        assert item.swallow_lines == [12, 40]

    def test_the_codec_writes_them_as_numbers(self):
        params = Neo4jRowCodec().to_node_params(_op().to_item())
        for field, value in COUNTED.items():
            assert params[field] == value, f"{field} not in the graph params"
        assert params["swallow_lines"] == [12, 40]

    def test_absent_on_the_wire_becomes_zero_not_a_crash(self):
        """An older changeset predates these fields; loading it must still work."""
        payload = {
            "op": "upsert_item",
            "id": "item-2",
            "type": "file",
            "name": "a.py",
            "path": "src/a.py",
            "content": "x",
        }
        params = Neo4jRowCodec().to_node_params(UpsertItemOp.model_validate(payload).to_item())
        for field in COUNTED:
            assert params[field] == 0, field
        assert params["swallow_lines"] == []

    @pytest.mark.parametrize("field", sorted(COUNTED))
    def test_a_nonzero_value_is_never_flattened(self, field):
        """Guards the failure mode specifically: 0 where a real number was sent."""
        params = Neo4jRowCodec().to_node_params(_op(**{field: 42}).to_item())
        assert params[field] == 42
