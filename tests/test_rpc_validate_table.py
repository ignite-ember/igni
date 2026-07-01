"""Tests for ``protocol/rpc.validate_rpc_table`` — the
"every RpcMethod enum value has a handler" safety net.

The function fires once at backend startup (after
``_build_rpc_table`` returns) so an added-enum-member-but-no-
registration mistake surfaces immediately rather than at first
call from the client. Catches the class of bug where:

  1. Someone adds ``RpcMethod.NEW_THING`` to the enum.
  2. They forget to add ``"new_thing": handler`` to the dispatch
     table in ``_build_rpc_table``.
  3. The FE calls ``new_thing`` → backend KeyError → wire-level
     error visible to the user.

Without ``validate_rpc_table``, step 3 is the first signal. With
it, the backend refuses to start.
"""

from __future__ import annotations

import pytest

from ember_code.protocol.rpc import RpcMethod, validate_rpc_table


def _all_method_values() -> set[str]:
    """Every wire string registered on the enum."""
    return {m.value for m in RpcMethod}


class TestPasses:
    def test_complete_table_passes(self):
        # Sanity — the full set never raises.
        validate_rpc_table(_all_method_values())

    def test_accepts_extra_unregistered_keys(self):
        # Extra keys are fine — the check is one-way (every
        # enum member must be there; the table may have more).
        # Useful for ad-hoc / debug RPCs that aren't in the
        # enum.
        validate_rpc_table(_all_method_values() | {"_debug_dump"})


class TestAcceptsIterable:
    """The signature is ``Iterable[str]`` — pin a few shapes
    so a future caller passing a generator or dict.keys() works."""

    def test_accepts_list(self):
        validate_rpc_table(list(_all_method_values()))

    def test_accepts_set(self):
        validate_rpc_table(_all_method_values())

    def test_accepts_dict_keys(self):
        # The real call site passes ``dispatch_table.keys()``
        # — pin this shape specifically.
        table = {v: None for v in _all_method_values()}
        validate_rpc_table(table.keys())

    def test_accepts_generator(self):
        validate_rpc_table(v for v in _all_method_values())


class TestRaises:
    def test_empty_iterable_raises_with_full_missing_list(self):
        # The worst-case: literally nothing registered. The
        # error message must list ALL missing handlers so the
        # developer can fix in one pass.
        with pytest.raises(RuntimeError) as exc:
            validate_rpc_table([])
        msg = str(exc.value)
        # Every enum value should be named in the error.
        for method in _all_method_values():
            assert method in msg

    def test_missing_one_method_named_in_error(self):
        # The realistic case — one new enum member, one
        # missing entry in ``_build_rpc_table``. The error
        # must name the exact wire string the developer needs
        # to add.
        all_keys = _all_method_values()
        a_member = next(iter(all_keys))
        partial = all_keys - {a_member}
        with pytest.raises(RuntimeError) as exc:
            validate_rpc_table(partial)
        assert a_member in str(exc.value)

    def test_error_mentions_fix_location(self):
        # The error message names the file + function to fix.
        # This is the affordance — a developer hitting the
        # startup raise should know where to go.
        with pytest.raises(RuntimeError) as exc:
            validate_rpc_table([])
        msg = str(exc.value)
        # The source explicitly names ``_build_rpc_table`` and
        # ``backend/__main__.py``.
        assert "_build_rpc_table" in msg
        assert "backend/__main__.py" in msg

    def test_missing_keys_sorted_for_stable_error(self):
        # The error lists missing keys via ``sorted(missing)``
        # — stable order makes the error scannable + makes
        # diffs across CI runs comparable.
        all_keys = _all_method_values()
        # Pick three to drop (stable picks for deterministic
        # ordering assertion).
        all_keys_list = sorted(all_keys)
        drop = set(all_keys_list[:3])
        partial = all_keys - drop
        with pytest.raises(RuntimeError) as exc:
            validate_rpc_table(partial)
        # The repr of the sorted list lands in the error.
        sorted_drop = sorted(drop)
        msg = str(exc.value)
        # The first dropped key appears BEFORE the second in
        # the message (sorted-order pinning).
        idx0 = msg.find(repr(sorted_drop[0])[1:-1])
        idx1 = msg.find(repr(sorted_drop[1])[1:-1])
        assert idx0 >= 0 and idx1 >= 0
        assert idx0 < idx1


class TestEnumShape:
    """Sanity checks on the enum itself — guards against the
    StrEnum contract drifting (e.g. someone making it an IntEnum
    by accident, which would break the wire format)."""

    def test_all_values_are_strings(self):
        for m in RpcMethod:
            assert isinstance(m.value, str)

    def test_all_values_are_unique(self):
        values = [m.value for m in RpcMethod]
        assert len(values) == len(set(values))

    def test_no_empty_values(self):
        # A bare ``""`` enum value would silently match any
        # missing-handler dict access. Pin against the
        # accidental-blank.
        for m in RpcMethod:
            assert m.value != ""
