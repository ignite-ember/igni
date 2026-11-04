"""Path 2, end to end: an event's handler opens a chat and posts, and the run starts there.

``docs/CLOUD.md`` §6: igni posts nothing for a service's event; it hands
it to the extension's ``@ext.on_service_event`` handler, which acts
through its host calls. This starts a real extension process — the
``igni-extension`` SDK, as igni starts one — against a backend assembled
as the app assembles it, hands it an event the way the inbox does
(:class:`ExtensionEventHandler`), and checks what its handler's
``host.sessions.create(title=…)`` and ``host.sessions.chat(id).post(…)``
did: a titled session, a run started in it with the handler's text, and
the person's window where it was.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from ember_code.backend.service_event_delivery import ExtensionEventHandler
from ember_code.extensions.service_events import EventHandling, ServiceEvent
from ember_code.protocol import messages as msg
from tests.host_model_helpers import PYTHON, FakeSession, HostRig, extension_process_env

FIXTURE = Path(__file__).parent / "fixtures" / "host_extension" / "indexer.py"
EXTENSION = "acme.indexer"


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
    rig.install(EXTENSION, host=["sessions", "chat.post", "sessions.post"])
    assert rig.channel is not None
    env = await rig.channel.grant(EXTENSION, secrets.token_urlsafe(16))
    proc = await asyncio.create_subprocess_exec(
        PYTHON,
        str(FIXTURE),
        env=extension_process_env(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    process = Process(proc)
    try:
        assert (await process.line())["ready"] is True
        yield rig, process
    finally:
        await process.stop()
        await rig.stop()


def _event() -> ServiceEvent:
    return ServiceEvent(
        event_id="evt-1",
        extension_id=EXTENSION,
        client_id="svc-indexer",
        type="index.ready",
        payload={"repo": "acme/api"},
    )


async def test_the_handler_opens_a_titled_chat_and_its_post_starts_the_run(started: Any) -> None:
    rig, process = started
    window = await rig.window("w-main", session="boot")

    handled = await ExtensionEventHandler()(_event())
    did = await process.line()
    await rig.settle()

    assert handled == EventHandling(handled=True)
    assert did["event"] == "evt-1"
    chat_id = did["created"]
    assert chat_id and chat_id != "boot"
    runtime = rig.orchestrator.pool.find(chat_id)
    assert runtime is not None
    chat: FakeSession = runtime.backend
    assert chat.titles == ["Index: acme/api"]
    [(agent_text, _)] = chat.ran
    assert f'<extension-message from="{EXTENSION}">' in agent_text
    assert "The code index for acme/api is ready" in agent_text
    assert {m.session_id for m in rig.windows.of(msg.RunStarted)} == {chat_id}
    # The extension posted it, not its service: no ``from_service``.
    [echo] = [m for m in rig.windows.of(msg.UserMessageReceived) if m.session_id == chat_id]
    assert echo.extension_id == EXTENSION and echo.service_client_id == ""
    # Nothing moved the person.
    assert rig.session.ran == []
    assert rig.model.windows.session(window) == "boot"
