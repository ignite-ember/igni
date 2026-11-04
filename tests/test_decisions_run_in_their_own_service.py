"""Laya runs in igni's decisions service; the backend only asks it over HTTP.

Laya brings torch and its checkpoints: ~480 MB in every backend that
loaded it. Now it lives in its own process — beside a desktop backend,
started on first use and idling out, or once per node in the cloud —
and the backend never imports it. These pin that boundary, the local
service's life, its secret, and that anything short of an answer is
``DecisionsUnavailable`` (which the Bypass guard turns into asking).
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ember_code.core.decisions import Laya
from ember_code.core.decisions.laya import (
    DecisionsEndpoints,
    LocalDecisions,
    RemoteDecisions,
)
from ember_code.core.decisions.schemas import DecisionRequest, DecisionsUnavailable
from ember_code.decisions_server.__main__ import DecisionsServer
from ember_code.decisions_server.app import DecisionsApp
from ember_code.decisions_server.model import LayaModel

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "ember_code"
HEAVY = ("laya", "torch", "transformers")
REQUEST = DecisionRequest(
    contexts=[{"text": "it crashes"}],
    questions=[{"id": "urgent", "kind": "noul", "question": "Is `text` urgent?"}],
)


class TestTheBackendNeverCarriesLaya:
    def test_only_the_service_and_the_installer_s_prefetch_import_it(self) -> None:
        allowed = {PACKAGE / "prefetch_models.py"}
        offenders: list[str] = []
        for path in PACKAGE.rglob("*.py"):
            if "decisions_server" in path.parts or path in allowed:
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                    for name in names:
                        if name.split(".")[0] in HEAVY or name.startswith(
                            "ember_code.decisions_server"
                        ):
                            offenders.append(f"{path.relative_to(ROOT)}: {name}")
        assert offenders == []

    def test_a_started_backend_with_the_guard_and_host_decide_has_no_torch(self) -> None:
        script = (
            "import sys\n"
            "import ember_code.backend.__main__\n"
            "import ember_code.core.hooks.bypass_guard\n"
            "import ember_code.extensions.host.model\n"
            "from ember_code.core.decisions import Laya\n"
            "Laya.shared()\n"
            f"loaded = [m for m in {HEAVY!r} if m in sys.modules]\n"
            "assert not loaded, loaded\n"
            "assert 'ember_code.decisions_server' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr


class TestWhereTheServiceIs:
    def test_a_node_s_service_is_named_by_url_and_token(self, monkeypatch) -> None:
        monkeypatch.setenv("IGNI_DECISIONS_URL", "http://decisions.node:8730/")
        monkeypatch.setenv("IGNI_DECISIONS_TOKEN", "t")
        endpoint = DecisionsEndpoints.configured()
        assert isinstance(endpoint, RemoteDecisions)
        connection = endpoint.connection()
        assert (connection.base_url, connection.token) == ("http://decisions.node:8730", "t")

    def test_without_one_the_backend_starts_its_own(self, monkeypatch) -> None:
        monkeypatch.delenv("IGNI_DECISIONS_URL", raising=False)
        assert isinstance(DecisionsEndpoints.configured(), LocalDecisions)


class TestTheSecret:
    def _app(self) -> DecisionsApp:
        return DecisionsApp(LayaModel(), "right")

    async def test_a_call_without_the_secret_is_refused_and_unanswered(self) -> None:
        app = self._app()
        transport = httpx.ASGITransport(app=app.asgi)
        laya = Laya(RemoteDecisions("http://decisions", "wrong", transport=transport))
        with pytest.raises(DecisionsUnavailable, match="secret"):
            await laya.decide(REQUEST)

    async def test_health_needs_it_too(self) -> None:
        app = self._app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.asgi), base_url="http://d"
        ) as client:
            assert (await client.get("/v1/health")).status_code == 401
            ok = await client.get("/v1/health", headers={"authorization": "Bearer right"})
            assert ok.json() == {"ok": True, "loaded": False}

    def test_the_service_will_not_start_without_one(self) -> None:
        with pytest.raises(ValueError):
            DecisionsApp(LayaModel(), "")
        env = {k: v for k, v in os.environ.items() if k != "IGNI_DECISIONS_SECRET"}
        result = subprocess.run(
            [sys.executable, "-m", "ember_code.decisions_server", "--port", "0"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode != 0
        assert "IGNI_DECISIONS_SECRET" in result.stderr


class TestTheLocalService:
    @pytest.fixture
    def offline(self, monkeypatch, tmp_path) -> None:
        """No model on this "machine", and no network to fetch one."""
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    def test_it_starts_on_first_use_and_again_after_it_has_gone(self) -> None:
        local = LocalDecisions()
        first = local.connection()
        assert first.base_url.startswith("http://127.0.0.1:")
        assert local.connection().base_url == first.base_url
        process: Any = local._process  # noqa: SLF001 — the child under test
        process.kill()
        process.wait(10)
        second = local.connection()
        assert second.token == first.token
        local.lost()

    async def test_a_model_that_cannot_load_is_unavailable_never_a_guess(self, offline) -> None:
        local = LocalDecisions()
        try:
            with pytest.raises(DecisionsUnavailable, match="could not be loaded"):
                await Laya(local).decide(REQUEST)
        finally:
            local.lost()

    def test_it_exits_with_the_backend_that_started_it(self, tmp_path) -> None:
        script = (
            "from ember_code.core.decisions.laya import LocalDecisions\n"
            "local = LocalDecisions()\n"
            "local.connection()\n"
            "print(local._process.pid, flush=True)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        pid = int(result.stdout.strip())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.1)
        assert not _alive(pid)


class TestIdlingOut:
    async def test_an_unused_service_stops_and_a_busy_one_does_not(self) -> None:
        app = DecisionsApp(LayaModel(), "s")
        server = DecisionsServer(app, idle_seconds=0.2)
        server.IDLE_POLL_S = 0.05
        app.activity.in_flight = 1
        app.activity.last = time.monotonic() - 60
        watcher = asyncio.create_task(server._idle_out())  # noqa: SLF001
        await asyncio.sleep(0.2)
        assert not server._server.should_exit  # noqa: SLF001 — a call is still running
        app.activity.in_flight = 0
        await asyncio.wait_for(watcher, 2)
        assert server._server.should_exit  # noqa: SLF001


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        finished, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return _not_a_zombie(pid)
    return finished == 0


def _not_a_zombie(pid: int) -> bool:
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")
