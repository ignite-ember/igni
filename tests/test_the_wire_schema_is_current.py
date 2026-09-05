"""The FE↔BE wire snapshot must match the protocol it describes.

`clients/web/src/protocol/wire-contract.test.ts` asserts that every
field the web client reads exists in `wire-schema.json`. It was written
because four of eleven bugs in one verification sweep were silent
field-name mismatches — a renamed BE field, a blank panel, no error
anywhere.

That test is only as good as the snapshot, and the snapshot is generated
by `scripts/dump_wire_schema.py` and committed. Nothing checked it was
current.

**It was not.** The generator selected message classes with
`cls.__module__ != msg.__name__` — only those defined in
`protocol/messages.py` itself. Then `messages` became a re-export shim
over `protocol/schemas/`, every class's `__module__` became
`...schemas.be_events` and friends, and the filter excluded all fifty.
The script kept exiting 0, printing "0 messages" in a line nobody reads,
and writing an empty contract. So running it *destroyed* the snapshot —
which is why nobody had run it for two months, and why the FE test spent
those two months validating against a July fossil.

The guard's input generator failed silently, and a guard checking a
fossil passes exactly like one that works. That is R-10 with an extra
step: the thing that measured nothing was not the test but the file it
measured against.

Two rules, and the second is the one that would have caught it:

* **the committed snapshot matches a fresh generation.** Renaming a
  protocol field now fails here until somebody regenerates, which is
  what makes the FE test a live contract rather than a memory;
* **a generation is not empty.** The snapshot going to nearly zero is
  not a protocol that shrank; it is an enumeration that stopped
  matching. The script refuses to write it, and this asserts the floor
  independently — a check that only compares two things is happy when
  both are empty.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / 'scripts/dump_wire_schema.py'
_SNAPSHOT = _ROOT / 'clients/web/src/protocol/wire-schema.json'


def _fresh() -> dict:
    """Generate into a scratch copy, leaving the committed file alone."""
    original = _SNAPSHOT.read_text()
    try:
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=_ROOT,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        return json.loads(_SNAPSHOT.read_text())
    finally:
        _SNAPSHOT.write_text(original)


class TestTheGeneratorStillFindsTheProtocol:
    def test_it_finds_the_message_types(self):
        """The floor. The bug was a generation of zero, and a comparison
        against a fresh generation would have been perfectly happy if
        the committed file had also been empty."""
        fresh = _fresh()

        assert len(fresh['messages']) >= 40, sorted(fresh['messages'])
        assert len(fresh['rpc']) >= 4, sorted(fresh['rpc'])

    def test_it_finds_classes_that_live_in_submodules(self):
        """The specific regression. Every message class is now defined
        in `protocol/schemas/*` and re-exported through `messages`, so a
        generator filtering on the defining module finds none of them.
        """
        from ember_code.protocol import messages as msg

        defined_here = [
            name
            for name in dir(msg)
            if getattr(getattr(msg, name), '__module__', '') == msg.__name__
            and hasattr(getattr(msg, name), 'model_fields')
        ]

        assert not defined_here, (
            f'{defined_here} are defined in `messages` itself now. That is fine, but the '
            'generator must still pick up the re-exported ones — check its filter.'
        )
        assert len(_fresh()['messages']) >= 40, 'the re-exported classes are being missed again'

    def test_it_refuses_to_write_an_empty_contract(self):
        """A generator that can destroy its own output has to check its
        output. Without this, the failure mode is a silent exit 0 and a
        three-line file where the contract was."""
        source = _SCRIPT.read_text()

        assert 'refusing to write' in source
        assert 'raise SystemExit' in source


class TestTheSnapshotIsCurrent:
    def test_the_committed_file_matches_a_fresh_generation(self):
        committed = json.loads(_SNAPSHOT.read_text())
        fresh = _fresh()

        assert committed == fresh, (
            'clients/web/src/protocol/wire-schema.json is stale. The web suite validates '
            'the fields it reads against this file, so while it is stale that check is '
            'against a memory of the protocol rather than the protocol. Regenerate with '
            '`uv run python scripts/dump_wire_schema.py`.'
        )

    def test_the_snapshot_covers_what_the_web_test_reads(self):
        """The two halves have to be about the same thing.

        The web test lists, per message type, the fields it consumes. If
        it names a type the snapshot does not have, it is asserting
        nothing for that type — and it would pass.
        """
        contract = (_ROOT / 'clients/web/src/protocol/wire-contract.test.ts').read_text()
        snapshot = json.loads(_SNAPSHOT.read_text())

        import re

        # `  type_name: ["field", ...],` inside the MESSAGE_READS map.
        block = contract.split('MESSAGE_READS')[1].split('};')[0]
        named = set(re.findall(r'^\s{2}(\w+):\s*\[', block, re.M))

        assert named, 'no message types parsed out of the web test — has it changed shape?'
        missing = sorted(named - set(snapshot['messages']))

        assert not missing, (
            f'the web test names {missing}, which the snapshot does not describe — so those '
            f'assertions check nothing.'
        )
