"""igni, set up for an air-gapped network, reaches nothing outside it while it runs.

`test_nothing_phones_home.py` reads the source for hosts. This runs igni
and watches what it actually tries: a session starts, does what a session
start does, and answers one message, with every name lookup, connection
and child process seen by the egress guard (`tests/egress_guard.py`). The
model is real network code talking to a real HTTP server on 127.0.0.1, so
the request path is the one a customer's on-premises model gets.

## What one run covers

The `Session` path, driven the way the app drives it:

- settings loaded the CLI's way (`load_settings_from_options`), from a
  `~/.igni/config.yaml` an administrator would write, with `--no-web`;
- `Session(...)` construction;
- the boot background services the backend starts once the ready line is
  out — today that is the plugin-marketplace warmup (D11);
- the update check the interactive banner and the app's
  `check_for_update` RPC make at start (D9);
- the `SessionStart` hook, one turn through `handle_message`, and every
  background task those leave behind, drained before the guard closes.

## What it cannot cover

- The desktop app's Tauri updater (D10) is Rust, not this process.
- A Rust or C extension with networking of its own, and anything a child
  process does once started (the guard sees the command line only).
- The websocket backend (`BackendApp`) and its streaming turn: this drives
  the `Session` beneath it. The stub serves streaming too, so a test of
  that path can reuse it.
- Tools the model never called: the stub answers in text, so web fetch
  and search (D12, D13) are shown denied by configuration, not exercised.

## The one known attempt

The marketplace warmup clones `github.com/anthropics/claude-plugins-official`
on a fresh install (D11), and nothing turns it off. Until an `air_gapped`
setting exists to do so, the run sees exactly that one attempt, and the
tests say so precisely: one asserts it is the *only* attempt, the other
asserts *no* attempt and is a strict xfail naming D11. When `air_gapped`
lands in `Settings`, the config below sets it, the first test stands down
and the second must pass outright.

TODO(D11): `MarketplaceWarmupPhase` has no off switch; an air-gapped
deployment needs one (the `air_gapped` setting) before the strict xfail
below can be removed.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from pydantic import BaseModel, ConfigDict

from ember_code.cli.invocation import load_settings_from_options
from ember_code.cli.options import CliOptions
from ember_code.core.activity import background
from ember_code.core.config.settings import Settings
from ember_code.core.paths import CONFIG_DIR
from ember_code.core.session.core import Session
from ember_code.core.utils.update_checker import check_for_update
from tests.egress_guard import EgressGuard, EgressLog, InternalNetwork

#: Whether the schema has the one switch that would turn D11 off.
AIR_GAPPED_SETTING_EXISTS = "air_gapped" in Settings.model_fields

#: The default marketplace a fresh install clones at session start (D11).
MARKETPLACE_REPO = "github.com/anthropics/claude-plugins-official"

REPLY = "Nothing left the building."


class ChatRequest(BaseModel):
    """The part of an OpenAI chat request the stub answers to."""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    stream: bool = False


class StubModel:
    """An OpenAI-compatible `/v1/chat/completions` on 127.0.0.1.

    A real server on a real socket, so the client's own HTTP stack makes
    the request, and the guard sees it as the internal call it is.
    Streams when asked to, the way a vLLM or llama.cpp server does.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.paths: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> StubModel:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def completion(self, model: str) -> ChatCompletion:
        return ChatCompletion(
            id="chatcmpl-stub",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(role="assistant", content=self.reply),
                )
            ],
            usage=CompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    def chunks(self, model: str) -> list[ChatCompletionChunk]:
        def chunk(delta: ChoiceDelta, finish: str | None) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id="chatcmpl-stub",
                object="chat.completion.chunk",
                created=int(time.time()),
                model=model,
                choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish)],  # type: ignore[arg-type]
            )

        return [
            chunk(ChoiceDelta(role="assistant", content=self.reply), None),
            chunk(ChoiceDelta(), "stop"),
        ]


class _StubHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
        stub: StubModel = self.server.stub  # type: ignore[attr-defined]
        stub.paths.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        request = ChatRequest.model_validate(json.loads(self.rfile.read(length) or b"{}"))
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        if request.stream:
            self._send_stream(stub.chunks(request.model))
        else:
            self._send_json(stub.completion(request.model).model_dump_json().encode())

    def _send_json(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self, chunks: list[ChatCompletionChunk]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {chunk.model_dump_json()}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class AirGappedInstall:
    """A fresh install, configured the way an air-gapped customer configures it.

    The settings are the YAML an administrator writes into
    `~/.igni/config.yaml`, loaded through the CLI's own loader: the
    configuration is under test as much as the code. With an `air_gapped`
    setting that is the one line `air_gapped: true`, nothing else; without
    one it is every switch that exists today — `update_check_ttl: 0`, the
    web tools denied, and `--no-web` on the command line.
    """

    def __init__(self, home: Path, project_dir: Path) -> None:
        self.home = home
        self.project_dir = project_dir

    def configure(self, model_url: str) -> Settings:
        config = self.home / CONFIG_DIR / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(self._config_yaml(model_url))
        return load_settings_from_options(CliOptions(no_web=not AIR_GAPPED_SETTING_EXISTS))

    @staticmethod
    def _config_yaml(model_url: str) -> str:
        switches = (
            "air_gapped: true\n"
            if AIR_GAPPED_SETTING_EXISTS
            else ("update_check_ttl: 0\npermissions:\n  web_fetch: deny\n  web_search: deny\n")
        )
        return (
            f"{switches}"
            "models:\n"
            "  default: on-prem\n"
            "  registry:\n"
            "    on-prem:\n"
            "      provider: openai_like\n"
            "      model_id: on-prem/model\n"
            f"      url: {model_url}\n"
            "      api_key: stub-key\n"
        )

    async def run(self, message: str) -> AirGappedRun:
        """Start a session as the app does and answer one message, under the guard."""
        with StubModel(REPLY) as model:
            settings = self.configure(model.url)
            guard = EgressGuard(network=InternalNetwork(), watch_subprocesses=True)
            with guard.watching() as log:
                response = await self._start_and_answer(settings, message)
            return AirGappedRun(
                settings=settings, response=response, model_paths=model.paths, log=log
            )

    async def _start_and_answer(self, settings: Settings, message: str) -> str:
        session = Session(settings, project_dir=self.project_dir)
        session.start_boot_background_services()  # D11, as BackendApp does after ready
        await check_for_update()  # D9, as the banner and the RPC do
        await session.fire_session_start_hook()
        response = await session.handle_message(message)
        await background.drain()
        return response


class AirGappedRun(BaseModel):
    """What one run produced, and what the guard saw while it ran."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    response: str
    model_paths: list[str]
    log: EgressLog


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AirGappedInstall:
    # Nothing on the developer's machine may widen what counts as internal.
    monkeypatch.delenv("IGNI_EGRESS_ALLOW_HOSTS", raising=False)
    # Air-gapped mode comes from the config file under test, not the shell.
    monkeypatch.delenv("IGNI_AIR_GAPPED", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    return AirGappedInstall(Path.home(), project)


async def test_the_run_is_real(install: AirGappedInstall) -> None:
    """A guard watching a run that never reached the network proves nothing."""
    run = await install.run("Say something.")

    if AIR_GAPPED_SETTING_EXISTS:
        assert run.settings.air_gapped is True  # type: ignore[attr-defined]
    else:
        assert run.settings.update_check_ttl == 0
    assert run.settings.permissions.web_fetch == "deny"
    assert run.settings.permissions.web_search == "deny"
    assert "/v1/chat/completions" in run.model_paths, run.model_paths
    assert REPLY in run.response


@pytest.mark.skipif(
    AIR_GAPPED_SETTING_EXISTS,
    reason="`air_gapped` exists; the test below asserts no attempt at all",
)
async def test_the_marketplace_clone_is_the_only_attempt(
    install: AirGappedInstall,
) -> None:
    """Every switch an air-gapped install has today is set, and one flow is left.

    D9 (update check) is off, D12/D13 (web tools) are denied, the model is
    on 127.0.0.1. What remains is D11: the session-start warmup clones the
    default plugin marketplace, and there is no setting to stop it. This
    asserts that it is that clone and nothing else — a second destination,
    or the clone disappearing, both fail here.
    """
    run = await install.run("Say something.")
    attempts = run.log.attempts

    assert attempts, "D11 was expected: the marketplace clone made no attempt"
    assert {a.event for a in attempts} == {"subprocess.Popen"}, run.log.report()
    assert all(
        a.destination.startswith("github.com ") and MARKETPLACE_REPO in a.destination
        for a in attempts
    ), run.log.report()


@pytest.mark.xfail(
    not AIR_GAPPED_SETTING_EXISTS,
    strict=True,
    reason=(
        f"D11: the session-start plugin-marketplace warmup clones {MARKETPLACE_REPO} "
        "and has no off switch; it needs the `air_gapped` setting"
    ),
)
async def test_an_air_gapped_session_reaches_nothing_outside_the_network(
    install: AirGappedInstall,
) -> None:
    run = await install.run("Say something.")

    assert not run.log.attempts, run.log.report()
