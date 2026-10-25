"""What an extension changes, where two collide, and how a decision applies.

An extension may take over a great deal — the whole of the agent's
prompt, if a deployment wants that — so "what happens when two want the
same thing" has one answer, given in three places: igni server's
Services page, igni server's distribution, and this host. The cases live
in ``tests/fixtures/extension_impact_cases.json`` so igni server runs the
very same ones against its own implementation; this file runs them here,
and fails when the copy beside a sibling ember-server checkout differs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.impact import ExtensionImpact, ImpactResolver, Resolution
from ember_code.extensions.manifest import ExtensionManifest

FIXTURE = Path(__file__).parent / "fixtures" / "extension_impact_cases.json"
CASES: dict[str, Any] = json.loads(FIXTURE.read_text())
SERVER_COPY = (
    Path(__file__).resolve().parents[2]
    / "ember-server"
    / "tests"
    / "fixtures"
    / "extension_impact_cases.json"
)


def _impacts(case: dict[str, Any]) -> list[ExtensionImpact]:
    return [ExtensionImpact.of(ExtensionManifest.model_validate(m)) for m in case["manifests"]]


def _resolver(case: dict[str, Any]) -> ImpactResolver:
    return ImpactResolver(
        _impacts(case), [Resolution.model_validate(r) for r in case["resolutions"]]
    )


def test_an_extension_s_impact_is_read_off_its_manifest():
    expected = dict(CASES["impact"]["expected"])
    claims = expected.pop("claims")
    impact = ExtensionImpact.of(ExtensionManifest.model_validate(CASES["impact"]["manifest"]))
    assert impact.model_dump(mode="json") == expected
    assert impact.claims() == claims


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_a_super_admin_is_shown_every_collision_to_decide(case):
    survey = _resolver(case).survey()
    assert [c.model_dump() for c in survey.collisions] == case["survey"]["collisions"]
    assert [c.model_dump() for c in survey.undecided] == case["survey"]["undecided"]


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_the_host_switches_off_what_lost_and_what_nobody_decided(case):
    outcome = _resolver(case).enforce()
    off = {ext: [o.claim for o in entries] for ext, entries in outcome.off.items()}
    assert off == case["enforce"]["off"]
    for ext, claims in case["enforce"]["off"].items():
        for claim in claims:
            assert not outcome.allows(ext, claim)


def test_every_switched_off_claim_says_why():
    case = next(c for c in CASES["cases"] if c["resolutions"] and c["survey"]["undecided"])
    outcome = _resolver(case).enforce()
    reasons = [o.reason for entries in outcome.off.values() for o in entries]
    assert any("decided by admin@acme.test on 2026-10-25" in r for r in reasons)
    assert any("no super admin has decided" in r for r in reasons)


@pytest.mark.skipif(not SERVER_COPY.is_file(), reason="no ember-server checkout beside this one")
def test_igni_servers_copy_of_the_cases_is_the_same():
    assert json.loads(SERVER_COPY.read_text()) == CASES


class TestTheManifestSaysWhereAStandInLives:
    BASE = {
        "id": "acme.a",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "python": {"package": "a", "entry": "a", "mcp": {"transport": "stdio"}},
    }

    def _with(self, **contributes):
        return {**self.BASE, "contributes": contributes}

    def test_a_stand_in_needs_a_plugin(self):
        with pytest.raises(ValueError, match="declares no 'plugin'"):
            ExtensionManifest.model_validate(
                self._with(prompt={"agents": [{"agent": "explorer", "from": "x/explorer.md"}]})
            )

    def test_a_stand_in_lives_inside_the_plugin(self):
        with pytest.raises(ValueError, match="not inside the plugin"):
            ExtensionManifest.model_validate(
                self._with(
                    plugin="plugin",
                    prompt={"agents": [{"agent": "explorer", "from": "elsewhere/explorer.md"}]},
                )
            )

    def test_a_stand_in_is_not_one_of_the_plugin_s_own_agents(self):
        """Everything in ``agents/`` loads as the plugin's own, under the
        extension's namespace; a stand-in there would load twice."""
        with pytest.raises(ValueError, match="stand-ins"):
            ExtensionManifest.model_validate(
                self._with(
                    plugin="plugin",
                    prompt={"agents": [{"agent": "explorer", "from": "plugin/agents/explorer.md"}]},
                )
            )

    def test_one_extension_claims_each_thing_once(self):
        target = {"target": "main", "source": "file", "from": "a.md"}
        with pytest.raises(ValueError, match="twice"):
            ExtensionManifest.model_validate(self._with(prompt={"replace": [target, target]}))
