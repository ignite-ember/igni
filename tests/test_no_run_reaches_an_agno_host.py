"""No agent run igni makes tries to reach Agno's own servers.

``test_no_run_is_reported_to_a_third_party.py`` checks the switch
(``AGNO_TELEMETRY``) and where igni sets it. This runs real agent turns
against a model on 127.0.0.1 and watches every name lookup and connection
the process attempts (``tests/egress_guard.py``), so a telemetry post that
the switch failed to stop — a new Agno code path, a construction site that
reads something else — shows up as the attempt it is.

The first test is the control: with ``AGNO_TELEMETRY=true`` and an Agent
built the way Agno's own examples build one, the guard sees the attempt
on ``os-api.agno.com``. Without it the others would pass on a guard that
sees nothing.

The hosts are Agno's own: the telemetry API (``os-api.agno.com``, and
``api-stg.agno.com`` for its staging runtime) and the AgentOS control
plane (``os.agno.com``). Any ``*.agno.com`` counts.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from agno.agent import Agent
from agno.models.openai.like import OpenAILike

from ember_code.cli.invocation import load_settings_from_options
from ember_code.cli.options import CliOptions
from ember_code.core.agents.schemas import AgentConstructorArgs
from ember_code.core.agno_telemetry import AgnoTelemetry
from ember_code.core.paths import CONFIG_DIR
from ember_code.core.session.core import Session
from tests.egress_guard import EgressGuard, EgressLog, InternalNetwork
from tests.test_igni_reaches_nothing_outside_the_network import StubModel

REPLY = "Nothing went to Agno."
MODEL_ID = "on-prem/model"


def agno_attempts(log: EgressLog) -> list[str]:
    return sorted(
        {
            a.describe()
            for a in log.attempts
            if a.destination.split(" ")[0].rstrip(".").lower().endswith("agno.com")
        }
    )


def stub_model(stub: StubModel) -> OpenAILike:
    return OpenAILike(id=MODEL_ID, base_url=stub.url, api_key="stub-key")


def write_config(home: Path, model_url: str, *, air_gapped: bool) -> None:
    config = home / CONFIG_DIR / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        ("air_gapped: true\n" if air_gapped else "") + "update_check_ttl: 0\n"
        "plugins:\n"
        "  default_marketplace: false\n"
        "models:\n"
        "  default: on-prem\n"
        "  registry:\n"
        "    on-prem:\n"
        "      provider: openai_like\n"
        f"      model_id: {MODEL_ID}\n"
        f"      url: {model_url}\n"
        "      api_key: stub-key\n"
    )


async def igni_turn(project_dir: Path, *, air_gapped: bool) -> tuple[str, EgressLog]:
    """One message through igni's own session, as the app sends it."""
    with StubModel(REPLY) as stub:
        write_config(Path.home(), stub.url, air_gapped=air_gapped)
        settings = load_settings_from_options(CliOptions(no_web=True))
        # What BackendApp and the CLI do once settings are loaded.
        AgnoTelemetry.enforce(os.environ, air_gapped=settings.air_gapped)
        with EgressGuard(network=InternalNetwork()).watching() as log:
            session = Session(settings, project_dir=project_dir)
            response = await session.handle_message("Say something.")
            # Agno 3 would post from a worker thread after the run returns
            # (``agno.api.api``): give it the time to, while still watched.
            for _ in range(20):
                if agno_attempts(log):
                    break
                await asyncio.sleep(0.05)
        assert "/v1/chat/completions" in stub.paths, stub.paths
    return response, log


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("IGNI_EGRESS_ALLOW_HOSTS", raising=False)
    monkeypatch.delenv("IGNI_AIR_GAPPED", raising=False)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


# The suite's own guard stands down for this one test, because the attempt
# is its subject: the guard below still refuses it, so nothing leaves.
@pytest.mark.reaches_the_internet
async def test_the_guard_sees_agno_telemetry_when_it_is_on(monkeypatch):
    monkeypatch.setenv("AGNO_TELEMETRY", "true")
    with StubModel(REPLY) as stub:
        agent = Agent(model=stub_model(stub))
        with EgressGuard(network=InternalNetwork()).watching() as log:
            await agent.arun("Say something.")
            # Agno 3 posts its telemetry from a worker thread of its own
            # (``agno.api.api``), after the run returns: wait for it.
            for _ in range(100):
                if agno_attempts(log):
                    break
                await asyncio.sleep(0.05)

    attempts = agno_attempts(log)
    assert attempts, "the control failed: Agno's telemetry made no attempt the guard could see"
    assert any("os-api.agno.com" in a for a in attempts), attempts


async def test_an_igni_turn_reaches_no_agno_host(project):
    assert os.environ.get("AGNO_TELEMETRY") == "false"

    response, log = await igni_turn(project, air_gapped=False)

    assert REPLY in response
    assert agno_attempts(log) == [], log.report()


async def test_an_opt_in_is_overruled_when_air_gapped(project, monkeypatch, caplog):
    monkeypatch.setenv("AGNO_TELEMETRY", "true")

    response, log = await igni_turn(project, air_gapped=True)

    assert REPLY in response
    assert os.environ["AGNO_TELEMETRY"] == "false"
    assert "air-gapped" in caplog.text
    assert agno_attempts(log) == [], log.report()


async def test_an_agent_igni_builds_sends_nothing_without_the_variable(monkeypatch):
    """``telemetry=False`` on the constructor is the floor under the switch."""
    monkeypatch.delenv("AGNO_TELEMETRY", raising=False)
    with StubModel(REPLY) as stub:
        args = AgentConstructorArgs(
            name="reviewer",
            model=stub_model(stub),
            description="",
            instructions=None,
            tools=None,
            num_history_runs=1,
            retries=0,
        )
        agent = Agent(**args.to_agno_kwargs())
        with EgressGuard(network=InternalNetwork()).watching() as log:
            output = await agent.arun("Say something.")

    assert REPLY in str(output.content)
    assert agno_attempts(log) == [], log.report()
