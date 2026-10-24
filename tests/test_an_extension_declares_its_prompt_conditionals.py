"""An extension declares which prompt sections it turns on.

A prompt carries `{{#name}}…{{/name}}` sections kept or dropped by what
the session can do. igni used to know the answers because it owned the
capabilities. It does not own them any more and cannot know which
extensions will be installed — the same as it cannot know which
plugins will be — so a capability *declares* the conditionals it
satisfies and igni merges whatever it is told.

Tested here as a contract, with fictional extensions, never a real
one: whether a particular extension declares the right names is that
extension's own test to write.
"""

from __future__ import annotations

from ember_code.core.prompts.conditionals import ConditionalRegistry
from ember_code.extensions.contributions import ExtensionContributions
from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.registry import ExtensionRegistry, ExtensionState
from ember_code.extensions.rpc import ExtensionRpc


class TestDeclaringOne:
    def test_a_declared_conditional_goes_live(self):
        registry = ConditionalRegistry()
        registry.declare("acme.notes", ["artifacts", "can-persist"])
        assert registry.live() == {"artifacts", "can-persist"}

    def test_two_owners_can_satisfy_the_same_one(self):
        """Neither has to know about the other: a second store does not
        need the first one's permission to say there is somewhere to
        persist to."""
        registry = ConditionalRegistry()
        registry.declare("acme.docs", ["can-persist"])
        registry.declare("acme.notes", ["can-persist"])
        assert registry.owners_of("can-persist") == ["acme.docs", "acme.notes"]
        registry.remove_owner("acme.docs")
        assert registry.live() == {"can-persist"}

    def test_a_capability_that_dies_stops_answering(self):
        """The rule prompt anchors and agent variants already
        follow: an extension whose service is down must stop
        telling the model about data it cannot fetch."""
        registry = ConditionalRegistry()
        registry.declare("acme.notes", ["artifacts"])
        registry.remove_owner("acme.notes")
        assert registry.live() == frozenset()

    def test_redeclaring_replaces(self):
        """What an upgrade does. An extension that dropped a
        conditional should stop answering yes to it."""
        registry = ConditionalRegistry()
        registry.declare("acme.notes", ["artifacts", "can-persist"])
        registry.declare("acme.notes", ["artifacts"])
        assert registry.live() == {"artifacts"}

    def test_declaring_nothing_removes_the_owner(self):
        registry = ConditionalRegistry()
        registry.declare("acme.notes", ["artifacts"])
        registry.declare("acme.notes", [])
        assert registry.owners_of("artifacts") == []


def _manifest(extension_id: str, conditionals: list[str]) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "contributes": {"promptConditionals": conditionals},
        }
    )


class TestThroughTheContributionsLifecycle:
    """What a declaration does once an extension is installed."""

    def _wired(self):
        registry = ExtensionRegistry()
        conditionals = ConditionalRegistry()
        contributions = ExtensionContributions(
            registry=registry, rpc=ExtensionRpc(registry), conditionals=conditionals
        )
        return registry, conditionals, contributions

    async def test_ready_turns_its_sections_on(self):
        registry, conditionals, contributions = self._wired()
        manifest = _manifest("acme.notes", ["notes", "can-persist"])
        registry.install(manifest, state=ExtensionState.READY)
        await contributions.apply(manifest)
        assert conditionals.live() == {"notes", "can-persist"}

    async def test_two_extensions_do_not_clobber_each_other(self):
        registry, conditionals, contributions = self._wired()
        for ident, names in (
            ("acme.notes", ["can-persist"]),
            ("acme.wiki", ["can-persist", "wiki"]),
        ):
            manifest = _manifest(ident, names)
            registry.install(manifest, state=ExtensionState.READY)
            await contributions.apply(manifest)
        contributions.withdraw("acme.notes")
        assert conditionals.live() == {"can-persist", "wiki"}
