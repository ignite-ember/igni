"""A producer's VM holds its own credential and keeps a token from it.

igni server's service-identity §16, ``igni_cloud/guest/credential.py``.
Against a fake igni server that rotates the credential on every exchange
and revokes the family when a rotated-away one comes back.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from igni_cloud.guest.credential import (
    CredentialFile,
    CredentialState,
    StateChange,
    VmCredentialExchange,
    VmCredentials,
)
from igni_cloud.guest.files import AtomicFile

from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig

SESSION = "the-persons-session"


class FakeIgni:
    """``POST /v1/oauth/token`` with the VM grants, rotating as igni server does."""

    def __init__(self) -> None:
        self.generation = 0
        self.current: str | None = None
        self.rotated_away: set[str] = set()
        self.revoked = False
        self.grants: list[dict[str, str]] = []
        self.in_flight = 0
        self.most_at_once = 0
        self.down = False

    def _issue(self) -> httpx.Response:
        self.generation += 1
        if self.current is not None:
            self.rotated_away.add(self.current)
        self.current = f"igvm1.org.cred.{self.generation}.secret"
        return httpx.Response(
            200,
            json={
                "access_token": f"token-{self.generation}",
                "token_type": "Bearer",
                "expires_in": 900,
                "vm_credential": self.current,
            },
        )

    @staticmethod
    def _invalid(why: str) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": why})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        self.in_flight += 1
        self.most_at_once = max(self.most_at_once, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.grants.append(form)
            if form["grant_type"].endswith("vm-sign-in"):
                if form.get("subject_token") != SESSION:
                    return self._invalid("only a person's session")
                self.revoked = False
                self.current = None
                return self._issue()
            presented = form.get("vm_credential", "")
            if presented in self.rotated_away:
                self.revoked = True  # reuse: two holders
                return self._invalid("reused")
            if self.revoked or presented != self.current:
                return self._invalid("revoked")
            return self._issue()
        finally:
            self.in_flight -= 1


class Vm:
    def __init__(
        self, tmp_path: Path, producer: str = "person:u-ada", igni: FakeIgni | None = None
    ) -> None:
        self.igni = igni if igni is not None else FakeIgni()
        self.credential = CredentialFile(tmp_path / "vol" / "vm-credential")
        self.credential.path.parent.mkdir(parents=True, exist_ok=True)
        self.token = tmp_path / "run" / "token"
        self.changes: list[StateChange] = []
        self.keeper = VmCredentials(
            credential_file=self.credential,
            token_file=self.token,
            exchange=VmCredentialExchange(
                lambda: "https://igni.test", transport=httpx.MockTransport(self.igni.handle)
            ),
            producer=producer,
            on_state=self.changes.append,
        )


async def test_a_person_signs_in_once_and_the_vm_keeps_its_credential(tmp_path: Path):
    vm = Vm(tmp_path)
    await vm.keeper.start()
    assert vm.keeper.state is CredentialState.NEEDS_SIGN_IN, "bound, nobody signed in yet"
    assert not vm.token.exists()

    change = await vm.keeper.sign_in(SESSION, "org-acme")
    assert change.state is CredentialState.READY
    assert vm.igni.grants[0]["organisation"] == "org-acme"
    assert vm.credential.read() == vm.igni.current
    assert stat.S_IMODE(vm.credential.path.stat().st_mode) == 0o600
    assert vm.token.read_text() == "token-1"
    await vm.keeper.stop()


async def test_every_exchange_rotates_and_the_new_credential_is_kept(tmp_path: Path):
    vm = Vm(tmp_path)
    await vm.keeper.sign_in(SESSION)
    first = vm.credential.read()
    await vm.keeper.refresh()
    assert vm.igni.grants[-1] == {
        "grant_type": "urn:igni:params:oauth:grant-type:vm-credential",
        "vm_credential": first,
    }
    assert vm.credential.read() == vm.igni.current != first
    assert vm.token.read_text() == "token-2"
    await vm.keeper.stop()


async def test_the_new_credential_is_written_before_the_token_is(tmp_path: Path, monkeypatch):
    vm = Vm(tmp_path)
    order: list[str] = []
    write_credential = CredentialFile.write
    write_file = AtomicFile.write

    def credential_first(self: CredentialFile, credential: str) -> None:
        order.append("credential")
        write_credential(self, credential)

    def token_after(self: AtomicFile, data: bytes) -> None:
        if self.path == vm.token:
            order.append("token")
        write_file(self, data)

    monkeypatch.setattr(CredentialFile, "write", credential_first)
    monkeypatch.setattr(AtomicFile, "write", token_after)
    await vm.keeper.sign_in(SESSION)
    await vm.keeper.refresh()
    assert order == ["credential", "token", "credential", "token"]
    await vm.keeper.stop()


async def test_a_crash_between_the_exchange_and_the_write_costs_the_credential(
    tmp_path: Path, monkeypatch
):
    """The documented outcome. igni server rotated the credential when it
    answered; the VM died before writing the answer down, so the volume
    still holds the rotated-away one. Presenting it again is reuse: igni
    server revokes the whole family, and the person signs in again."""
    vm = Vm(tmp_path)
    await vm.keeper.sign_in(SESSION)
    kept = vm.credential.read()

    def crash(self: CredentialFile, credential: str) -> None:
        raise SystemExit("the VM died here")

    monkeypatch.setattr(CredentialFile, "write", crash)
    with pytest.raises(SystemExit):
        await vm.keeper.refresh()
    monkeypatch.undo()
    assert vm.credential.read() == kept, "the old one is all the volume has"

    restarted = Vm(tmp_path, igni=vm.igni)  # the same volume, the same igni server
    await restarted.keeper.start()
    assert restarted.keeper.state is CredentialState.NEEDS_SIGN_IN
    assert vm.igni.revoked, "reuse revoked the family"
    assert not restarted.token.exists(), "no token the server no longer stands behind"
    await restarted.keeper.stop()


async def test_a_revoked_credential_means_sign_in_for_a_person_and_reissue_for_an_app(
    tmp_path: Path,
):
    person = Vm(tmp_path / "p")
    await person.keeper.sign_in(SESSION)
    person.igni.revoked = True  # signed out everywhere, removed, deactivated
    assert (await person.keeper.refresh()).state is CredentialState.NEEDS_SIGN_IN
    assert [c.state for c in person.changes][-1] is CredentialState.NEEDS_SIGN_IN

    app = Vm(tmp_path / "a", producer="app:reporter")
    app.igni.current = "igvm1.org.app.1.secret"
    app.credential.write(app.igni.current)  # what the node agent placed on the volume
    await app.keeper.start()
    assert app.keeper.state is CredentialState.READY
    app.igni.revoked = True  # the app was deleted
    assert (await app.keeper.refresh()).state is CredentialState.NEEDS_REISSUE
    await person.keeper.stop()
    await app.keeper.stop()


async def test_an_apps_credential_is_taken_when_it_appears_without_a_restart(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(VmCredentials, "FILE_POLL_SECONDS", 0.02)
    vm = Vm(tmp_path, producer="app:reporter")
    await vm.keeper.start()
    assert vm.keeper.state is CredentialState.NEEDS_REISSUE, "bound, no credential yet"
    vm.igni.current = "igvm1.org.app.1.secret"
    vm.credential.write(vm.igni.current)  # the node agent, late
    for _ in range(200):
        if vm.keeper.state is CredentialState.READY:
            break
        await asyncio.sleep(0.01)
    assert vm.keeper.state is CredentialState.READY
    assert vm.token.read_text() == "token-1"
    await vm.keeper.stop()


async def test_one_exchange_at_a_time(tmp_path: Path):
    vm = Vm(tmp_path)
    await vm.keeper.sign_in(SESSION)
    vm.keeper._expires_at = 0.0  # due
    before = len(vm.igni.grants)
    await asyncio.gather(*(vm.keeper.refresh(force=False) for _ in range(5)))
    assert vm.igni.most_at_once == 1, "never two at once"
    assert len(vm.igni.grants) == before + 1, "the others found the token fresh"
    await vm.keeper.stop()


async def test_an_unreachable_server_keeps_the_last_token_and_tries_again(tmp_path: Path):
    vm = Vm(tmp_path)
    await vm.keeper.sign_in(SESSION)
    vm.igni.down = True
    assert (await vm.keeper.refresh()).state is CredentialState.UNREACHABLE
    assert vm.token.read_text() == "token-1", "kept until it expires"
    vm.igni.down = False
    assert (await vm.keeper.refresh()).state is CredentialState.READY
    await vm.keeper.stop()


async def test_the_gateway_hands_the_session_over_the_rpc(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    asked: list[tuple[str, str | None]] = []

    async def sign_in(token: str, organisation: str | None) -> Any:
        asked.append((token, organisation))
        return StateChange(state=CredentialState.READY)

    try:
        assert rig.orchestrator is not None
        rig.orchestrator.attach_vm_sign_in(sign_in)
        await rig.orchestrator.dispatch(
            msg.RPCRequest(
                id="s1", method="vm_sign_in", args={"session_token": SESSION, "organisation": "o"}
            )
        )
        await rig.orchestrator.dispatch(msg.RPCRequest(id="s2", method="vm_sign_in", args={}))
        answers = {m.id: m for m in rig.windows.of(msg.RPCResponse)}
        assert answers["s1"].result == {"state": "ready", "message": ""}
        assert answers["s2"].error and "session_token" in answers["s2"].error
        assert asked == [(SESSION, "o")]
    finally:
        await rig.stop()
