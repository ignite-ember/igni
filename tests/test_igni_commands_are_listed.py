"""``docs/igni-commands.json`` is every slash command igni answers itself.

No extension can have one of them: the dispatcher runs igni's first, so
an extension's ``/clear`` would never run. igni server reads its copy of
this list to refuse such a bundle at upload, and the host switches the
claim off (:class:`~ember_code.extensions.impact.IgniCommands`). Held to
the dispatcher here, because a list kept by hand is a list that is wrong.

To refresh it::

    python -c "from pathlib import Path; from ember_code.backend.command_handler import \\
CommandHandler; from ember_code.extensions.impact import IgniCommands; \\
Path('docs/igni-commands.json').write_text(IgniCommands.of(CommandHandler.builtin_names())\\
.model_dump_json(indent=2) + '\\n')"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.impact import ExtensionImpact, IgniCommands, ImpactResolver
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry

REPO = Path(__file__).resolve().parents[1]
LISTED = REPO / "docs" / "igni-commands.json"
SERVER_COPY = REPO.parent / "ember-server" / "app" / "services" / "identity" / "igni_commands.json"

MANIFEST = {
    "id": "acme.tidy",
    "version": "1.0.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "python": {"package": "acme_tidy", "entry": "acme-tidy", "mcp": {"transport": "stdio"}},
    "contributes": {
        "commands": [
            {"command": "/clear", "desc": "clears something else"},
            {"command": "/tidy", "desc": "tidies"},
        ]
    },
}


def test_the_list_is_what_the_dispatcher_answers():
    assert IgniCommands.load(LISTED) == IgniCommands.of(CommandHandler.builtin_names())


@pytest.mark.skipif(not SERVER_COPY.is_file(), reason="no ember-server checkout beside this one")
def test_igni_servers_copy_is_the_same():
    assert IgniCommands.load(SERVER_COPY) == IgniCommands.load(LISTED)


def test_an_extension_s_igni_command_is_switched_off_and_says_why():
    impact = ExtensionImpact.of(ExtensionManifest.model_validate(MANIFEST))
    igni = IgniCommands.load(LISTED)
    assert igni.clashes(impact) == ["/clear"]

    outcome = ImpactResolver([impact], reserved=igni.claims()).enforce()
    assert not outcome.allows("acme.tidy", "command:/clear")
    assert outcome.allows("acme.tidy", "command:/tidy")
    (off,) = outcome.off["acme.tidy"]
    assert "igni's own" in off.reason
    # Nothing between extensions, so nothing for a super admin to decide.
    assert outcome.collisions == []


def test_the_host_applies_it_without_a_decision():
    registry = ExtensionRegistry()
    contributions = ExtensionContributions(
        registry=registry,
        rpc=None,
        igni_commands=IgniCommands.of(CommandHandler.builtin_names()),
    )
    registry.install(ExtensionManifest.model_validate(MANIFEST))
    contributions.resolve()
    assert [o.claim for o in contributions.switched_off("acme.tidy")] == ["command:/clear"]
