"""Agno reports every run to its own API unless told not to.

Found by reading a ``--debug`` log during a smoke test, not by reading
code::

    httpx INFO HTTP Request: POST https://os-api.agno.com/telemetry/runs
        "HTTP/2 201 Created"

Seven such requests per run, measured — with ``AGNO_TELEMETRY=true`` a
single ``igni -m "Say OK."`` produced seven, and with the default this
package now sets it produces none.

**What it sends is metadata, not content.**
``agno.agent._telemetry.get_telemetry_data`` builds agent/session/run
ids, the provider, model name and model id, the database class name,
and a set of capability booleans. No prompt, no code, no paths. So the
landing page's "your code never leaves your network" was never false.

It is still wrong for this product:

* an unannounced outbound call from software sold as self-hosted, on
  every run, to a host the operator did not choose;
* it names the customer's **model** and **storage backend**, which is
  commercially sensitive on its own;
* it is a third party that appears in none of the documents that
  enumerate where data goes — the Article 30 record, the DPIA, the
  Statement of Applicability. An auditor finding an undeclared
  processor is a finding regardless of payload.

The switch is ``AGNO_TELEMETRY``, set by ``ember_code/__init__`` before
anything imports Agno (``core/agno_telemetry.py``):
``agno.agent._init.set_telemetry`` re-reads the variable on every run and
overrides whatever the constructor said, so the environment covers
construction sites that do not exist yet. Every ``Agent`` / ``Team`` igni
builds passes ``telemetry=False`` as well, for a process whose environment
lost the variable, and extension services — whose MCP stdio launch
inherits almost nothing — are handed it.

An explicit ``AGNO_TELEMETRY=true`` is honoured outside air-gapped mode,
deliberately: "nothing leaves by default, and every means to send it is
available" is the line the deployment documents take everywhere else.
Under air-gapped mode it is forced off with a warning. That the runs
really reach no Agno host is
``test_no_run_reaches_an_agno_host.py``, which watches the sockets.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ember_code.core.agents.schemas import AgentConstructorArgs
from ember_code.core.agno_telemetry import AgnoTelemetry
from ember_code.core.session.agent_builder.agent_build_spec import AgentBuildSpec
from ember_code.extensions.installer import ExtensionInstaller
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.permissions import Permissions

_ROOT = Path(__file__).resolve().parent.parent


def _env_after_import(preset: dict[str, str] | None = None) -> str | None:
    """``AGNO_TELEMETRY`` as it stands after importing the package.

    Run in a subprocess because the import is a one-time side effect:
    ``ember_code`` is already imported in this process, so asserting
    against the current environment would only measure whatever the
    test runner happened to inherit.
    """
    env = {**os.environ, **(preset or {})}
    if preset is None:
        env.pop("AGNO_TELEMETRY", None)
    if "IGNI_AIR_GAPPED" not in (preset or {}):
        env.pop("IGNI_AIR_GAPPED", None)
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, ember_code; print(os.environ.get('AGNO_TELEMETRY', '<unset>'))",
        ],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env=env,
        check=True,
    )
    return out.stdout.strip()


class TestTheDefaultIsSilence:
    def test_importing_the_package_disables_telemetry(self):
        value = _env_after_import()

        assert value != "<unset>", (
            "importing `ember_code` no longer sets AGNO_TELEMETRY. Agno's default is "
            "on, so every run will POST to os-api.agno.com."
        )
        # Agno's check is `telemetry_env.lower() == "true"`, so anything
        # else disables — but assert the honest value rather than
        # relying on that, since a future Agno could invert the test.
        assert value.lower() == "false", f"AGNO_TELEMETRY is {value!r}, which is not clearly off"

    def test_an_explicit_choice_is_honoured(self):
        """Opting in has to remain possible.

        `setdefault`, not assignment. An operator who wants to send
        telemetry can; the product simply does not decide that for
        them.
        """
        assert _env_after_import({"AGNO_TELEMETRY": "true"}) == "true"

    def test_an_explicit_off_is_left_alone(self):
        assert _env_after_import({"AGNO_TELEMETRY": "no"}) == "no"


class TestAnAirGappedProcessKeepsItOff:
    """Under air-gapped mode an explicit opt-in is overruled, with a warning."""

    def test_the_environment_switch_overrules_it_at_import(self):
        """A process whose air gap comes from the environment alone — the
        decisions service, a child of a backend — reads no settings."""
        preset = {"AGNO_TELEMETRY": "true", "IGNI_AIR_GAPPED": "1"}
        assert _env_after_import(preset) == "false"

    def test_the_setting_overrules_it_once_settings_are_loaded(self, caplog):
        environ = {"AGNO_TELEMETRY": "true"}
        decision = AgnoTelemetry.enforce(environ, air_gapped=True)

        assert environ["AGNO_TELEMETRY"] == "false"
        assert decision.overruled is True
        assert "air-gapped" in caplog.text

    def test_outside_the_air_gap_an_opt_in_stands(self):
        environ = {"AGNO_TELEMETRY": "true"}
        decision = AgnoTelemetry.enforce(environ, air_gapped=False)

        assert environ["AGNO_TELEMETRY"] == "true"
        assert decision.overruled is False

    def test_an_unset_variable_is_set_off(self):
        environ: dict[str, str] = {}
        AgnoTelemetry.enforce(environ, air_gapped=False)
        assert environ == {"AGNO_TELEMETRY": "false"}


class TestTheConstructorsSayItToo:
    """``telemetry=False`` wherever igni builds an Agent or Team, so a
    process whose environment lost the variable still sends nothing."""

    def test_the_main_agent_spec_passes_it(self):
        assert AgentBuildSpec.model_fields["telemetry"].default is False

    def test_a_defined_agent_passes_it(self):
        kwargs = AgentConstructorArgs(
            name="a",
            model=None,
            description="",
            instructions=None,
            tools=None,
            num_history_runs=1,
            retries=0,
        ).to_agno_kwargs()
        assert kwargs["telemetry"] is False

    def test_a_sub_team_passes_it(self):
        source = (_ROOT / "src/ember_code/core/tools/orchestrate_spawn.py").read_text()
        assert '"telemetry": AgnoTelemetry.CONSTRUCTOR_VALUE' in source

    def test_an_extension_service_is_handed_it_and_cannot_turn_it_on(self, tmp_path, monkeypatch):
        """An MCP stdio launch passes on only ``PATH``, ``HOME`` and a few
        more, so an extension built on Agno would run with Agno's default."""
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        installer = ExtensionInstaller.__new__(ExtensionInstaller)
        installer._project_dir = tmp_path
        manifest = ExtensionManifest.model_validate(
            {
                "id": "acme.scanner",
                "version": "1.0.0",
                "contract": ">=1.0 <2",
                "trust": "unsigned",
                "dev": {"command": ["node", "server.js"], "env": {"AGNO_TELEMETRY": "true"}},
            }
        )
        env = installer._process_environment(dict(manifest.dev.env), Permissions(manifest), {})

        assert env["AGNO_TELEMETRY"] == "false"
