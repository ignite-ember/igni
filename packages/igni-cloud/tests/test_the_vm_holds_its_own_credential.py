"""A producer's VM holds its own credential and keeps a token from it.

igni server's service-identity §16, ``igni_cloud/guest/credential.py``.
Against a fake igni server that rotates the credential on every exchange
and revokes the family when a rotated-away one comes back.
"""

from __future__ import annotations

import asyncio
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest.credential import (
    CredentialFile,
    CredentialState,
    StateChange,
    VmCredentialExchange,
    VmCredentials,
)
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.resume import NextAnswer, RestoreWatch

from ember_code.core.auth.http_auth import CloudTokenAuth
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


class WallClock:
    """The guest's wall clock, moved by the test (a pause is a jump)."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Wake:
    """``after_wake``: held until the test says the first answer and its burst are through."""

    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.through = asyncio.Event()
        self.burst: bool | None = None

    async def __call__(self, *, burst: bool = True) -> None:
        self.burst = burst
        self.waiting.set()
        await self.through.wait()


class Vm:
    def __init__(
        self,
        tmp_path: Path,
        producer: str = "person:u-ada",
        igni: FakeIgni | None = None,
        *,
        clock: WallClock | None = None,
        wake: Wake | None = None,
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
            after_wake=wake,
            clock=clock if clock is not None else time.time,
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


async def test_a_renewal_keeps_the_event_loop_free(tmp_path: Path, monkeypatch):
    """A renewal due while the VM was paused runs as it is unpaused, beside
    the request that woke it: the credential's fsyncs (network storage)
    must not hold that request's handshake."""
    vm = Vm(tmp_path)
    await vm.keeper.sign_in(SESSION)
    write_credential = CredentialFile.write

    def slow_volume(self: CredentialFile, credential: str) -> None:
        time.sleep(0.3)
        write_credential(self, credential)

    monkeypatch.setattr(CredentialFile, "write", slow_volume)
    ticks: list[float] = []

    async def the_handshake() -> None:
        for _ in range(20):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    renewal = asyncio.ensure_future(vm.keeper.refresh())
    await the_handshake()
    assert (await renewal).state is CredentialState.READY
    assert vm.token.read_text() == "token-2"
    assert max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.2, "the loop ran"
    await vm.keeper.stop()


async def test_one_client_for_every_exchange(tmp_path: Path):
    """Not a client (and its TLS context) made on the event loop per renewal."""
    vm = Vm(tmp_path)
    await vm.keeper.sign_in(SESSION)
    client = vm.keeper._exchange.client()
    await vm.keeper.refresh()
    assert vm.keeper._exchange.client() is client
    await vm.keeper.stop()


@pytest.fixture
def kept(monkeypatch: pytest.MonkeyPatch, cloud_layer: Any) -> Any:
    """The cloud layer reads the keeper's token file; the keeper is forgotten after."""

    def keep(vm: Vm) -> None:
        monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(vm.token))
        EnvironmentCredentials.kept_by(vm.keeper)

    yield keep
    EnvironmentCredentials.kept_by(None)


async def _paused_past_the_renewal(vm: Vm, clock: WallClock) -> None:
    """Signed in; the renewal armed; then the VM paused past it (the wall clock jumps)."""
    await vm.keeper.sign_in(SESSION)
    vm.keeper._arm(0.05)  # the 14-minute timer, shortened
    clock.now += 1000.0  # paused: the token (900 s) has expired
    await asyncio.sleep(0.1)  # unpaused: the timer finds itself overdue


async def test_a_renewal_due_during_a_pause_waits_for_the_first_answer(tmp_path: Path):
    clock, wake = WallClock(), Wake()
    vm = Vm(tmp_path, clock=clock, wake=wake)
    await _paused_past_the_renewal(vm, clock)
    assert wake.waiting.is_set(), "overdue by the wall clock: it waits"
    assert wake.burst is False, "expired: renewed as soon as the first answer is out"
    assert len(vm.igni.grants) == 1, "nothing sent beside the request that woke the VM"
    wake.through.set()
    for _ in range(100):
        if len(vm.igni.grants) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(vm.igni.grants) == 2, "renewed once the first answer is through"
    assert vm.token.read_text() == "token-2"
    assert vm.credential.read() == vm.igni.current
    await vm.keeper.stop()


async def test_a_token_with_life_left_waits_for_the_burst_too(tmp_path: Path, kept: Any):
    clock, wake = WallClock(), Wake()
    vm = Vm(tmp_path, clock=clock, wake=wake)
    kept(vm)
    await vm.keeper.sign_in(SESSION)
    vm.keeper._arm(0.05)
    clock.now += 30.0  # overdue by the wall clock, the token still good
    await asyncio.sleep(0.1)
    assert wake.waiting.is_set() and wake.burst is True
    assert EnvironmentCredentials.token() == "token-1", "still handed out meanwhile"
    wake.through.set()
    await vm.keeper.stop()


async def test_a_timer_on_time_renews_at_once(tmp_path: Path):
    clock, wake = WallClock(), Wake()
    vm = Vm(tmp_path, clock=clock, wake=wake)
    await vm.keeper.sign_in(SESSION)
    vm.keeper._arm(0.02)
    for _ in range(100):
        if len(vm.igni.grants) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(vm.igni.grants) == 2
    assert not wake.waiting.is_set(), "no pause: nothing to wait for"
    await vm.keeper.stop()


async def test_an_expired_token_is_never_handed_out(tmp_path: Path, kept: Any):
    clock, wake = WallClock(), Wake()
    vm = Vm(tmp_path, clock=clock, wake=wake)
    kept(vm)
    await vm.keeper.sign_in(SESSION)
    assert EnvironmentCredentials.token() == "token-1"
    clock.now += 900.0 - VmCredentials.SEND_MARGIN_SECONDS + 1  # about to expire
    assert vm.token.read_text() == "token-1", "still in the file"
    assert EnvironmentCredentials.token() == "", "but not handed out"
    assert not EnvironmentCredentials().load().ok
    await vm.keeper.stop()


async def test_a_call_before_the_renewal_renews_at_once_and_only_once(tmp_path: Path, kept: Any):
    clock, wake = WallClock(), Wake()
    vm = Vm(tmp_path, clock=clock, wake=wake)
    kept(vm)
    await _paused_past_the_renewal(vm, clock)
    assert wake.waiting.is_set()
    # A model call made right after the wake: CloudTokenAuth waits until it may send.
    sent: list[str | None] = []

    async def model(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        auth=CloudTokenAuth(), transport=httpx.MockTransport(model)
    ) as client:
        await client.post("https://igni.test/v1/chat/completions", json={})
    assert sent == ["Bearer token-2"], "a fresh token, never the expired one"
    assert len(vm.igni.grants) == 2, "renewed at once for the call"
    wake.through.set()
    await asyncio.sleep(0.05)
    assert len(vm.igni.grants) == 2, "the deferred renewal found it fresh: no second rotation"
    assert vm.credential.read() == vm.igni.current
    assert not vm.igni.revoked
    await vm.keeper.stop()


async def test_calls_during_a_renewal_wait_for_it(tmp_path: Path, kept: Any):
    clock = WallClock()
    vm = Vm(tmp_path, clock=clock)
    kept(vm)
    await vm.keeper.sign_in(SESSION)
    clock.now += 1000.0
    await asyncio.gather(*(EnvironmentCredentials().ready() for _ in range(5)))
    assert vm.igni.most_at_once == 1
    assert len(vm.igni.grants) == 2, "one renewal for all five"
    assert EnvironmentCredentials.token() == "token-2"
    await vm.keeper.stop()


async def test_the_next_answer_and_its_burst(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.2)
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 0.05)
    answers = NextAnswer()
    answers.answered()  # one before the wake does not count
    waiting = asyncio.ensure_future(answers.after())
    await asyncio.sleep(0.02)
    assert not waiting.done()
    started = time.monotonic()
    answers.answered()
    await waiting
    assert 0.04 <= time.monotonic() - started < 0.15, "the burst after it"
    started = time.monotonic()
    await answers.after()
    assert time.monotonic() - started >= 0.19, "no answer: a moment, then on"
    waiting = asyncio.ensure_future(answers.after(burst=False))
    await asyncio.sleep(0.01)
    answers.answered()
    started = time.monotonic()
    await waiting
    assert time.monotonic() - started < 0.04, "without the burst: at the answer"


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
