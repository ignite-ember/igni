"""What an extension changes, where two collide, and who wins by priority.

An extension may take over a great deal — the whole of the agent's
prompt, if a deployment wants that — so "what happens when two want the
same thing" has one answer: the extension higher in the super admin's
priority order. It is given in two places, igni server's Services page
and this host. The cases live
in ``tests/fixtures/extension_impact_cases.json`` so igni server runs the
very same ones against its own implementation; this file runs them here,
and fails when the copy beside a sibling ember-server checkout differs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.impact import ExtensionImpact, ImpactResolver
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
    return ImpactResolver(_impacts(case), case["priority"], reserved=case.get("reserved", []))


def test_an_extension_s_impact_is_read_off_its_manifest():
    expected = dict(CASES["impact"]["expected"])
    claims = expected.pop("claims")
    items = expected.pop("items")
    impact = ExtensionImpact.of(ExtensionManifest.model_validate(CASES["impact"]["manifest"]))
    assert impact.model_dump(mode="json") == expected
    assert impact.claims() == claims
    assert impact.items() == items


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_every_collision_goes_to_the_higher_priority(case):
    outcome = _resolver(case).resolve()
    assert [c.model_dump() for c in outcome.collisions] == case["collisions"]
    off = {ext: [o.claim for o in entries] for ext, entries in outcome.off.items()}
    assert off == case["off"]
    dropped = {ext: [o.claim for o in entries] for ext, entries in outcome.dropped.items()}
    assert dropped == case["dropped"]
    for ext, claims in case["off"].items():
        for claim in claims:
            assert not outcome.allows(ext, claim)


def test_a_loss_by_priority_names_who_won():
    case = next(c for c in CASES["cases"] if c["name"].startswith("each collision is settled"))
    outcome = _resolver(case).resolve()
    reasons = [o.reason for o in outcome.off["acme.a"]]
    assert reasons and all(r.startswith("lower priority than acme.b") for r in reasons)


def test_an_outranked_exclusive_override_says_which_editor_beat_it():
    case = next(c for c in CASES["cases"] if c["name"].startswith("an exclusive override below"))
    (entry,) = _resolver(case).resolve().off["acme.a"]
    assert entry.reason.startswith("lower priority than acme.b, which edits main")


@pytest.mark.skipif(not SERVER_COPY.is_file(), reason="no ember-server checkout beside this one")
def test_igni_servers_copy_of_the_cases_is_the_same():
    assert json.loads(SERVER_COPY.read_text()) == CASES


def test_a_drop_says_whose_override_and_when():
    case = next(c for c in CASES["cases"] if "keeps its frontmatter" in c["name"])
    (entry,) = _resolver(case).resolve().dropped["acme.b"]
    assert "acme.a overrides agent:explorer while acme.a holds indexed" in entry.reason
    assert "no section 'rules'" in entry.reason


class TestTheManifestSaysWhatItDoesToAPrompt:
    BASE = {
        "id": "acme.a",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "python": {"package": "a", "entry": "a", "mcp": {"transport": "stdio"}},
    }

    def _with(self, **contributes):
        return {**self.BASE, "contributes": contributes}

    @staticmethod
    def _edit(**op):
        return {"target": "main", "source": "file", "from": "a.md", **op}

    def test_an_agent_override_is_not_one_of_the_plugin_s_own_agents(self):
        """Everything in ``agents/`` loads as the plugin's own, under the
        extension's namespace; an override there would load twice."""
        override = {
            "target": "agent:explorer",
            "sections": ["intro"],
            "source": "file",
            "from": "plugin/agents/explorer.md",
        }
        with pytest.raises(ValueError, match="overrides/"):
            ExtensionManifest.model_validate(
                self._with(plugin="plugin", prompt={"overrides": [override]})
            )

    def test_an_edit_is_exactly_one_change(self):
        with pytest.raises(ValueError, match="exactly one of"):
            ExtensionManifest.model_validate(
                self._with(prompt={"edits": [self._edit(replace="workflow", append="tail")]})
            )

    def test_an_insert_says_where(self):
        with pytest.raises(ValueError, match="'before' and 'after'"):
            ExtensionManifest.model_validate(
                self._with(prompt={"edits": [self._edit(insert="notes")]})
            )

    def test_an_override_says_its_sections(self):
        override = {"target": "main", "sections": [], "source": "file", "from": "a.md"}
        with pytest.raises(ValueError):
            ExtensionManifest.model_validate(self._with(prompt={"overrides": [override]}))

    def test_overriding_and_editing_one_prompt_is_refused(self):
        override = {"target": "main", "sections": ["identity"], "source": "file", "from": "a.md"}
        with pytest.raises(ValueError, match="both overrides and edits"):
            ExtensionManifest.model_validate(
                self._with(prompt={"overrides": [override], "edits": [self._edit(append="tail")]})
            )

    def test_one_extension_replaces_each_section_once(self):
        edit = self._edit(replace="workflow")
        with pytest.raises(ValueError, match="twice"):
            ExtensionManifest.model_validate(self._with(prompt={"edits": [edit, edit]}))

    def test_an_extension_that_runs_no_process_cannot_hook_an_event(self):
        """igni asks over the host channel of a process it starts; an endpoint never connects."""
        manifest = {key: value for key, value in self.BASE.items() if key != "python"} | {
            "endpoint": {"url": "https://acme.test/mcp"},
            "permissions": {"hooks": ["PreToolUse"]},
        }
        with pytest.raises(ValueError, match="needs a process igni starts"):
            ExtensionManifest.model_validate(manifest)
