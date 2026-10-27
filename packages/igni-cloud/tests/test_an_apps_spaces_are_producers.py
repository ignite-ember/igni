"""An app's spaces — one per end user — are producers of their own: ``space:<id>``.

The producer string is the token's kind and id, written as
``IGNI_PRODUCER`` and given to ``grant-producer``; a space's app is its
owner (``IGNI_PRODUCER_OWNER``, ``--owner``). igni server is settling the
rest of the contract; these hold igni's side of what is known.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from igni_cloud.db.admin_cli import db
from igni_cloud.guest.binding import ProducerBinding
from igni_cloud.guest.credential import (
    CredentialFile,
    CredentialState,
    VmCredentialExchange,
    VmCredentials,
)
from igni_cloud.producer import ProducerKind, ProducerRef, UnknownProducer

from ember_code.core.db.scope import Tenant


def _token(**claims: Any) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


def test_the_three_kinds_and_a_spaces_owner():
    space = ProducerRef.parse("space:s-42", owner="app:reporter")
    assert (space.kind, space.id, space.owner, space.ref) == (
        ProducerKind.SPACE,
        "s-42",
        "app:reporter",
        "space:s-42",
    )
    assert space.unattended and ProducerRef.parse("app:reporter").unattended
    assert not ProducerRef.parse("person:u-ada").unattended
    for bad, owner in (
        ("robot:x", None),
        ("space", None),
        ("person:u", "app:a"),
        ("space:s", "person:u"),
    ):
        with pytest.raises(UnknownProducer):
            ProducerRef.parse(bad, owner=owner)


def test_granting_refuses_a_kind_it_does_not_know():
    result = CliRunner().invoke(
        db,
        [
            "grant-producer",
            "--org",
            "o",
            "--producer",
            "robot:x",
            "--database-url",
            "postgresql://a@b/c",
        ],
    )
    assert result.exit_code != 0 and "kind" in result.output


def test_a_space_binds_with_its_token_naming_it(cloud_layer: Any, tmp_path: Path, monkeypatch):
    for name in ("IGNI_ACCESS_TOKEN_FILE", "IGNI_ACCESS_TOKEN", "IGNI_ORG", "IGNI_PRODUCER"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    token = tmp_path / "token"
    token.write_text(_token(sub="s-42", kind="space", org="org-acme", exp=int(time.time()) + 600))
    env = tmp_path / "producer.env"
    env.write_text(
        "IGNI_DATABASE_URL=sqlite:///x.db\nIGNI_ORG=org-acme\nIGNI_PRODUCER=space:s-42\n"
        f"IGNI_PRODUCER_OWNER=app:reporter\nIGNI_ACCESS_TOKEN_FILE={token}\n"
    )
    attempt = ProducerBinding(env, environ={}).attempt()
    assert attempt.bound and attempt.tenant == Tenant(org="org-acme", producer="space:s-42")
    assert attempt.env is not None and attempt.env.owner == "app:reporter"


async def test_a_spaces_vm_without_a_credential_needs_reissue_not_sign_in(tmp_path: Path):
    keeper = VmCredentials(
        credential_file=CredentialFile(tmp_path / "vm-credential"),
        token_file=tmp_path / "token",
        exchange=VmCredentialExchange(lambda: "https://igni.test"),
        producer="space:s-42",
    )
    await keeper.start()
    assert keeper.state is CredentialState.NEEDS_REISSUE
    await keeper.stop()
