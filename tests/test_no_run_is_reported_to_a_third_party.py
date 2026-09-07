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

The fix is one ``os.environ.setdefault`` in ``ember_code/__init__``
rather than ``telemetry=False`` at each construction site. There are a
dozen of those across agents, pools, ephemeral builders and the
orchestrator; ``agno.agent._init.set_telemetry`` re-reads the variable
on every run and overrides whatever the constructor said, so the
environment covers paths that do not exist yet. The repository already
passed ``telemetry=False`` in ``core/evals/assertion_runner.py`` — the
idiom was known, it just had not been applied where users are.

An explicit value is honoured, deliberately. "Nothing leaves by
default, and every means to send it is available" is the same line the
deployment documents take everywhere else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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


class TestTheMechanismIsStillTheOneAgnoReads:
    """Guards the fix against Agno moving the switch.

    The environment override lives in ``agno.agent._init.set_telemetry``
    and is what makes a single assignment cover every construction
    site. If that function stops reading the variable, this package's
    default becomes decorative and every run starts reporting again —
    silently, because nothing else would change.
    """

    def test_agno_still_reads_the_environment_variable(self):
        from agno.agent import _init

        source = Path(_init.__file__).read_text()

        assert "AGNO_TELEMETRY" in source, (
            "agno no longer reads AGNO_TELEMETRY in `_init`. Find where telemetry is "
            "switched now and set it there, or pass telemetry=False at every "
            "Agent/Team construction site."
        )

    def test_the_telemetry_payload_is_still_metadata_only(self):
        """The claim in this module's docstring, checked against the code.

        If Agno starts including prompts or file paths, "metadata, not
        content" stops being true and the finding changes from a
        compliance one to a confidentiality one.
        """
        from agno.agent import _telemetry

        source = Path(_telemetry.__file__).read_text()

        for leaked in ("messages", "input", "prompt", "content"):
            assert f'"{leaked}"' not in source, (
                f"agno telemetry now sends {leaked!r}. This is no longer metadata; "
                "the disable above is load-bearing for confidentiality, not just for "
                "the processor list."
            )
