"""Schema-level tests for :class:`SessionEvent`.

The construction path from ``Session.append_event`` is exercised
in ``test_event_log.py``; this file locks in the model's own
invariants (validation, defaults, wire round-trip, defensive
payload copy) so future edits to the schema fail loud rather
than silently drifting the shape.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ember_code.core.session.event_log_schema import SessionEvent


class TestBuild:
    def test_build_preserves_fields(self):
        event = SessionEvent.build(
            seq=42,
            event_type="visualization_delta",
            payload={"spec_id": "s1", "json": "{}"},
            run_id="run-x",
        )
        assert event.seq == 42
        assert event.type == "visualization_delta"
        assert event.run_id == "run-x"
        assert event.payload == {"spec_id": "s1", "json": "{}"}

    def test_build_defaults_run_id_to_empty(self):
        event = SessionEvent.build(seq=1, event_type="x", payload={})
        assert event.run_id == ""

    def test_build_coerces_run_id_to_str(self):
        # ``run_id`` accepts anything with a ``__str__``; None → "".
        event = SessionEvent.build(seq=1, event_type="x", payload={}, run_id=None)  # type: ignore[arg-type]
        assert event.run_id == ""
        event2 = SessionEvent.build(seq=1, event_type="x", payload={}, run_id=123)  # type: ignore[arg-type]
        assert event2.run_id == "123"

    def test_build_copies_payload_defensively(self):
        # Mutating the caller's dict after build must not affect
        # the stored payload. Matches the pre-refactor behaviour in
        # ``Session.append_event`` (dict(payload)).
        source = {"a": 1}
        event = SessionEvent.build(seq=1, event_type="x", payload=source)
        source["a"] = 999
        assert event.payload == {"a": 1}


class TestValidation:
    def test_extra_field_rejected(self):
        # ``extra="forbid"`` — a stray field means producer drift
        # and should fail loud.
        with pytest.raises(ValidationError):
            SessionEvent(
                seq=1,
                type="x",
                payload={},
                unexpected_field="value",  # type: ignore[call-arg]
            )

    def test_seq_must_be_positive(self):
        with pytest.raises(ValidationError):
            SessionEvent(seq=0, type="x", payload={})

    def test_type_required(self):
        with pytest.raises(ValidationError):
            SessionEvent(seq=1, payload={})  # type: ignore[call-arg]

    def test_payload_defaults_to_empty_dict(self):
        event = SessionEvent(seq=1, type="x")
        assert event.payload == {}

    def test_timestamp_ms_auto_populated(self):
        event = SessionEvent(seq=1, type="x")
        # Any positive int — we're not pinning the exact clock, just
        # that it's populated with something usable.
        assert isinstance(event.timestamp_ms, int)
        assert event.timestamp_ms > 0


class TestFromWire:
    def test_valid_dict_round_trips(self):
        wire = {
            "seq": 5,
            "run_id": "r1",
            "timestamp_ms": 1_700_000_000_000,
            "type": "visualization_delta",
            "payload": {"json": "abc"},
        }
        event = SessionEvent.from_wire(wire)
        assert event is not None
        assert event.seq == 5
        assert event.type == "visualization_delta"
        assert event.payload == {"json": "abc"}
        # Round-trip via ``model_dump`` produces the same shape.
        assert event.model_dump() == wire

    def test_malformed_returns_none(self):
        # A corrupt row from the DB shouldn't crash the log load.
        # Both "wrong type" and "missing required" cases return None.
        assert SessionEvent.from_wire({"not": "an event"}) is None
        assert SessionEvent.from_wire({"seq": "not-an-int"}) is None

    def test_extra_field_returns_none(self):
        # ``extra="forbid"`` propagates through from_wire — a producer
        # drift should surface as a dropped row rather than a
        # silently-passed-through unknown field.
        wire = {
            "seq": 1,
            "run_id": "",
            "timestamp_ms": 0,
            "type": "x",
            "payload": {},
            "unknown": "value",
        }
        assert SessionEvent.from_wire(wire) is None


class TestModelDump:
    def test_dump_matches_pre_refactor_wire_shape(self):
        # The pre-iter-45 ``append_event`` built dicts of exactly
        # this shape. ``.model_dump()`` must produce the same keys /
        # types so the splicer + persistence path see no change.
        event = SessionEvent.build(
            seq=1,
            event_type="visualization_delta",
            payload={"spec_id": "s1", "json": "{}"},
            run_id="r1",
        )
        dumped = event.model_dump()
        assert set(dumped.keys()) == {"seq", "run_id", "timestamp_ms", "type", "payload"}
        assert dumped["seq"] == 1
        assert dumped["run_id"] == "r1"
        assert dumped["type"] == "visualization_delta"
        assert dumped["payload"] == {"spec_id": "s1", "json": "{}"}
        assert isinstance(dumped["timestamp_ms"], int)
