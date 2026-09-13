"""The menu may not advertise a command the backend cannot run.

The composer carried a hand-written list of thirty-two commands beside
a ``get_slash_commands`` RPC that returns the authoritative forty. Both
directions had drifted:

* ``/workflows`` was offered and does not exist in the backend — that
  one is fine, it is intercepted client-side, and it is now declared as
  such rather than merely tolerated.
* Nine real commands were undiscoverable from the menu: ``/commit``,
  ``/pr``, ``/explain``, ``/resolve-issues``, ``/watcher``,
  ``/test-plan``, ``/migration``, ``/gpu-ai``, ``/exit``.

The frontend takes the backend's list at runtime now, so the drift
cannot recur in the direction that matters. What remains is the
fallback list — used while the RPC is in flight and against a backend
too old to answer — and a fallback that lies is still a lie. This
checks it, the same way ``wire-contract`` checks the message schema:
by reading the TypeScript and comparing it to the Python.
"""

from __future__ import annotations

import re
from pathlib import Path

COMPOSER = Path(__file__).parent.parent / "clients" / "web" / "src" / "components" / "Composer.tsx"


def _fallback_commands() -> set[str]:
    """Names from the composer's ``BUILTIN_COMMANDS`` array."""
    source = COMPOSER.read_text(encoding="utf-8")
    start = source.index("export const BUILTIN_COMMANDS")
    end = source.index("];", start)
    return set(re.findall(r'name:\s*"(/[a-z-]+)"', source[start:end]))


def _client_only() -> set[str]:
    """Names the frontend declares it handles itself."""
    source = COMPOSER.read_text(encoding="utf-8")
    start = source.index("export const CLIENT_ONLY_COMMANDS")
    end = source.index("];", start)
    return set(re.findall(r'"(/[a-z-]+)"', source[start:end]))


def _backend_commands() -> set[str]:
    """What the backend's builtin registry dispatches.

    The RPC also reports markdown commands and skills, which are
    per-project and per-user; the built-ins are the part a shipped
    frontend can reasonably know about.
    """
    # ``core.session`` first, deliberately.
    # ``builtin_command_registry`` cannot be imported before it:
    # importing the registry runs ``cmd_context`` → ``schemas_context``,
    # which rebuilds a pydantic model at import time, which pulls in
    # ``core.session`` — whose package ``__init__`` drags the whole
    # interactive stack back round to ``command_handler``, which then
    # asks the half-initialised registry for ``BUILTIN_REGISTRY``.
    #
    # So the module works only when something else has already
    # imported the cycle in a friendly order — an ordering contract
    # nobody wrote down and nothing enforces. ``core.session``,
    # ``backend.app`` and ``backend.server`` all work as the first
    # import; the registry and ``command_handler`` do not. Worth
    # untangling; not here, where it would be a large change riding on
    # a test.
    import ember_code.core.session  # noqa: F401 — see above
    from ember_code.backend.builtin_command_registry import BUILTIN_REGISTRY

    return {f"/{name.lstrip('/')}" for name in BUILTIN_REGISTRY.names()}


def test_the_fallback_advertises_nothing_the_backend_lacks():
    """The rule. A command in the menu has to go somewhere."""
    phantom = _fallback_commands() - _backend_commands() - _client_only()

    assert not phantom, (
        f"the composer offers {sorted(phantom)}, which the backend does not "
        "define. Either implement them, remove them, or — if the client "
        "handles them itself, as it does /workflows — add them to "
        "CLIENT_ONLY_COMMANDS with the intercept that justifies it."
    )


def test_client_only_commands_really_are_absent_from_the_backend():
    """The other direction: an exemption that stops being needed is a
    lie of a quieter kind, and it would hide a real conflict if the
    backend later took that name."""
    overlap = _client_only() & _backend_commands()

    assert not overlap, (
        f"{sorted(overlap)} is declared client-only but the backend now "
        "defines it too — the intercept and the command will fight."
    )


def test_the_fallback_is_not_empty():
    """Guards the parser, not the product: a regex that silently
    matched nothing would make both tests above pass forever."""
    assert len(_fallback_commands()) > 20


def test_the_frontend_prefers_the_backends_list():
    """The structural half of the fix. Without the RPC wired, the
    fallback is not a fallback — it is the list again, and this file
    would be guarding a copy nobody reads."""
    app = (COMPOSER.parent.parent / "App.tsx").read_text(encoding="utf-8")

    assert "get_slash_commands" in app
    assert "mergeCommands" in COMPOSER.read_text(encoding="utf-8")
