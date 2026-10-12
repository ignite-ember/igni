"""The permission resolver's web rows named functions nobody registers.

``ToolNameResolver._MAP`` translates the Agno function name a tool call
arrives with into the catalog name that permission rules are written in
(``Bash``, ``Write``, ``WebFetch``). Its three web rows were
``web_fetch``, ``duckduckgo_search`` and ``duckduckgo_news``. None of the
three is a function any toolkit registers: ``WebFetchSpec`` builds
``WebTools``, which registers ``fetch_url`` / ``fetch_json``, and
``WebSearchSpec`` builds ``DuckDuckGoTools``, which registers
``web_search`` / ``search_news`` — the spec's own docstring says so,
because the identical drift had already broken its confirmation gate.

So every real web call missed the table, ``resolve`` fell through to
returning the func_name unchanged, and HITL "always" / "allow similar"
wrote ``fetch_url(domain:github.com)`` into settings.json. The evaluator
matches catalog names, so that rule can never match anything: the user
answered the prompt, the rule persisted, and the next fetch prompted
again — permanently.

These tests read the expected names off the specs that build the
toolkits rather than restating them, so the next rename in Agno fails
here instead of silently re-opening the same hole. Restating the names
is exactly what the old tests did, which is why the drift survived.
"""

from __future__ import annotations

from ember_code.core.config.tool_permissions.tool_name_resolver import ToolNameResolver
from ember_code.core.tools.tool_spec import WebFetchSpec, WebSearchSpec


class TestTheMapMatchesTheRegistry:
    def test_every_web_fetch_function_resolves_to_webfetch(self):
        resolver = ToolNameResolver()
        spec = WebFetchSpec()

        assert spec.agno_function_names  # guards against an empty-tuple pass
        for func_name in spec.agno_function_names:
            assert resolver.resolve(func_name=func_name) == spec.name, func_name

    def test_every_web_search_function_resolves_to_websearch(self):
        resolver = ToolNameResolver()
        spec = WebSearchSpec()

        assert spec.agno_function_names
        for func_name in spec.agno_function_names:
            assert resolver.resolve(func_name=func_name) == spec.name, func_name

    def test_the_invented_names_are_gone(self):
        """``web_fetch`` / ``duckduckgo_search`` / ``duckduckgo_news``
        were never registered by anything. Leaving them in the table is
        not harmless: it is the thing that made the table look complete
        to every reader and every test that touched it."""
        mappings = ToolNameResolver.all_mappings()

        for invented in ("web_fetch", "duckduckgo_search", "duckduckgo_news"):
            assert invented not in mappings


class TestThePersistedRuleIsUsable:
    def test_a_fetch_call_canonicalises_to_the_catalog_name(self):
        """The user-visible consequence. ``resolve`` is what HITL
        "always" / "allow similar" runs before writing the rule; before
        the fix this returned ``"fetch_url"`` and the rule written from
        it could never match again."""
        assert ToolNameResolver().resolve(func_name="fetch_url") == "WebFetch"
        assert ToolNameResolver().resolve(func_name="web_search") == "WebSearch"

    def test_an_unknown_function_still_names_itself(self):
        """The fallback is deliberate and must survive: an unmapped
        function is safer named after itself than silently claimed to
        be a well-known tool."""
        assert ToolNameResolver().resolve(func_name="some_plugin_tool") == "some_plugin_tool"

    def test_an_explicit_tool_name_still_wins(self):
        assert ToolNameResolver().resolve(tool_name="WebFetch", func_name="fetch_url") == "WebFetch"
