"""The committed wire schema is what the generator produces.

The generator had been writing an empty schema. Its rule for "is this
a protocol message" was ``cls.__module__ == protocol.messages``, and
the schemas had since moved into ``protocol/schemas/*`` with
``messages.py`` left as a re-export shim — so every class failed the
test and the output contained **zero messages**.

Nothing caught it, because nothing ran it. The snapshot the frontend
contract-tests against was hand-edited from then on, and by the time
anyone looked it was missing ``event_seq`` from most messages: the
frontend was checking its reads against a picture of the protocol
that had stopped being true.

So the generator runs here, on every suite, and the file has to match.
"""

from __future__ import annotations

import json

from ember_code.protocol.wire_schema import SNAPSHOT_PATH, build_schema, render

REGENERATE = "uv run python scripts/dump_wire_schema.py"


def test_the_snapshot_matches_the_protocol():
    """The whole point. A field added, renamed or removed on a message
    fails here until the snapshot is regenerated."""
    assert SNAPSHOT_PATH.read_text() == render(), (
        f"{SNAPSHOT_PATH.name} is out of date with the protocol. Run:\n    {REGENERATE}"
    )


def test_the_generator_finds_messages_at_all():
    """The failure that actually happened, asserted directly.

    The schema-matches test above would have passed happily against an
    empty snapshot and an empty generator — both sides wrong in the
    same way is exactly how this stayed broken.
    """
    schema = build_schema()

    assert len(schema["messages"]) > 40, (
        "the generator found almost no messages — its idea of what counts "
        "as a protocol message has probably drifted from the code again"
    )


def test_the_generator_agrees_with_the_transport():
    """Everything the wire can carry is in the schema.

    ``MessageRegistry`` is what the transports consult to turn a
    ``type`` string into a class. A message it accepts but the schema
    omits is a message the frontend can receive and no test can check.
    """
    from ember_code.protocol.registry import MessageRegistry

    schema = build_schema()
    missing = set(MessageRegistry().known_types()) - set(schema["messages"])

    assert not missing, f"the transport accepts these but the schema omits them: {sorted(missing)}"


def test_the_snapshot_is_valid_json_with_both_halves():
    """Guards the file itself. It has been truncated by a stray shell
    redirect before now — the generator writes the file and prints a
    log line, so `> wire-schema.json` captures the log line and throws
    the schema away."""
    data = json.loads(SNAPSHOT_PATH.read_text())

    assert data["messages"]
    assert data["rpc"]
