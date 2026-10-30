"""A real extension process reaches the host object model over its socket.

``docs/HOST_OBJECT_MODEL.md``: the process uses a loopback WebSocket
guarded by its per-process secret. This starts one — a separate Python
process running the ``igni-extension`` SDK, as igni starts one — against
a backend assembled as the app assembles it, and checks that a signal
arrives, that its handler can read and write the model while handling
it, and that the socket admits nobody else.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from ember_code.extensions.host.channel import ENV_HOST_SECRET, ENV_HOST_URL
from ember_code.protocol import messages as msg
from tests.host_model_helpers import PYTHON, HostRig, extension_process_env

FIXTURE = Path(__file__).parent / "fixtures" / "host_extension" / "reviewer.py"


class Process:
    """The extension's process, read a JSON line at a time."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    async def line(self, timeout: float = 15.0) -> dict[str, Any]:
        assert self.proc.stdout is not None
        while True:
            raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            if not raw:
                stderr = await self.proc.stderr.read() if self.proc.stderr else b""
                raise AssertionError(f"the extension exited: {stderr.decode()[-2000:]}")
            text = raw.decode().strip()
            if text.startswith("{"):
                return json.loads(text)

    async def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


@pytest.fixture
async def started(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    rig.install("acme.reviewer", host=["sessions", "chat.read", "chat.append"], workspace="read")
    assert rig.channel is not None
    env = await rig.channel.grant("acme.reviewer", secrets.token_urlsafe(16))
    proc = await asyncio.create_subprocess_exec(
        PYTHON,
        str(FIXTURE),
        env=extension_process_env(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process = Process(proc)
    try:
        yield rig, process, env
    finally:
        await process.stop()
        await rig.stop()


async def test_the_process_connects_and_is_told_what_it_holds(started: Any) -> None:
    _, process, _ = started
    hello = await process.line()
    assert hello["ready"] is True
    assert set(hello["granted"]) >= {"sessions", "chat.read", "chat.append", "workspace"}
    assert hello["schema"]
    # An object it was not granted does not exist for it.
    assert await process.line() == {"refused": "not_granted"}


async def test_a_signal_arrives_and_its_handler_reads_and_writes_the_model(started: Any) -> None:
    rig, process, _ = started
    await process.line()
    await process.line()
    assert rig.model is not None

    # The person moves to another session: igni's own path for that.
    rig.model.session_switched("s-42", None)
    seen = await process.line()
    assert seen["signal"] == "current_changed"
    assert seen["session_id"] == "s-42"
    assert seen["processing"] is False
    # The handler's write reached the windows.
    await rig.settle()
    pushes = [
        p for p in rig.windows.pushes("orchestrate_event") if p.payload["spec_id"] == seen["view"]
    ]
    assert (
        pushes
        and json.loads(pushes[0].payload["json"])["elements"]["c"]["props"]["title"] == "s-42"
    )


async def test_a_broadcast_becomes_a_signal_in_the_process(started: Any) -> None:
    rig, process, _ = started
    await process.line()
    await process.line()
    await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r7"))
    await rig.transport.send(
        msg.PushNotification(
            session_id="boot", channel="file_edited", payload={"path": str(rig.project / "app.py")}
        )
    )
    await rig.transport.send(msg.RunCompleted(session_id="boot", run_id="r7"))
    seen = await process.line()
    assert seen == {"signal": "run_completed", "run_id": "r7", "changed_files": ["app.py"]}


async def test_the_socket_admits_only_the_process_it_was_granted_to(started: Any) -> None:
    _, _, env = started
    url = env[ENV_HOST_URL]
    with pytest.raises(InvalidStatus) as wrong:
        await connect(url, additional_headers={"Authorization": "Bearer not-the-secret"})
    assert wrong.value.response.status_code == 401
    # A browser — any page on any site can try a loopback port — is refused
    # even with the right secret, because it sends an Origin.
    with pytest.raises(InvalidStatus) as browser:
        await connect(
            url,
            additional_headers={"Authorization": f"Bearer {env[ENV_HOST_SECRET]}"},
            origin="https://evil.example",  # type: ignore[arg-type]
        )
    assert browser.value.response.status_code == 403


async def test_a_revoked_process_is_disconnected(started: Any) -> None:
    rig, process, env = started
    await process.line()
    await process.line()
    assert rig.channel is not None
    rig.channel.revoke("acme.reviewer")
    with pytest.raises(InvalidStatus):
        await connect(
            env[ENV_HOST_URL],
            additional_headers={"Authorization": f"Bearer {env[ENV_HOST_SECRET]}"},
        )
