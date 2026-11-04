"""How a tool from an MCP server is named once it is registered.

Namespacing is not optional. Without it a server's ``read_file``
shadows — or is shadowed by — the built-in of that name, depending
on registration order, and the asymmetry is silent.

The prefix therefore has to be built the same way by both
transports, and *read back* the same way by everything that
compares a registered name against the server's own. It was built
in two places and read back in none, which is why
``permissions.tools`` refused every tool it was asked about: the
installer handed ``mcp_ext:acme.scanner_list`` to a check written
against ``list``, nothing matched, and an extension's declared
tools were disabled along with its undeclared ones. Enforcement
that refuses everything looks like enforcement right up until
somebody declares something.

Its own module because the two builders cannot import each other:
``client`` owns ``stdio_binding``, so anything both need lives
below both.
"""

from __future__ import annotations


def tool_name_prefix(server: str) -> str:
    """What is prefixed onto every tool registered from ``server``.

    Without the trailing separator, which the tool registry adds.
    """
    return f"mcp_{server}"


def bare_tool_name(server: str, registered: str) -> str:
    """A registered tool name as the server's own name for it.

    The inverse of :func:`tool_name_prefix`. ``get_tools`` returns
    registered names while an MCP session — and the manifest an
    administrator reads — both speak the server's own, so anything
    comparing the two has to convert.

    A name without the prefix comes back unchanged: callers pass
    lists from mixed sources, and a bare name is already bare.
    """
    prefix = f"{tool_name_prefix(server)}_"
    return registered[len(prefix) :] if registered.startswith(prefix) else registered


__all__ = ["bare_tool_name", "tool_name_prefix"]
