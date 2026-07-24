"""Import-time check that :class:`PermissionModeName` (wire) and
:class:`PermissionMode` (domain) stay in sync.

The wire enum lives in
:mod:`ember_code.protocol.schemas.enums.PermissionModeName` — a
copy kept in the protocol leaf module so the wire schema doesn't
have to import the domain layer. The domain enum lives in
:mod:`ember_code.core.config.permission_eval.PermissionMode`.

If either enum grows a new member without the mirror being
updated, this test fails at import time — a much friendlier
signal than a runtime ``ValueError`` on the FE when the BE
starts emitting an un-mirrored mode name.
"""

from __future__ import annotations

from ember_code.core.config.permission_eval import PermissionMode
from ember_code.protocol.schemas.enums import PermissionModeName


def test_permission_mode_wire_and_domain_stay_in_sync() -> None:
    """Every domain :class:`PermissionMode` value MUST appear in
    :class:`PermissionModeName` (wire), and every wire value except
    the ``UNKNOWN`` safety valve MUST appear in the domain enum.

    The wire enum carries a forward-compat ``UNKNOWN`` member that
    the domain doesn't need (the domain always knows the current
    posture); the assertion excludes it.
    """
    domain_values = {m.value for m in PermissionMode}
    wire_values = {m.value for m in PermissionModeName if m is not PermissionModeName.UNKNOWN}
    assert domain_values == wire_values, (
        f"PermissionMode / PermissionModeName drift:\n"
        f"  domain-only: {domain_values - wire_values}\n"
        f"  wire-only  : {wire_values - domain_values}"
    )
