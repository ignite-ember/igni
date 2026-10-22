"""The extension system.

An extension is one concept with two packagings — a signed sidecar
image we supervise, or a remote endpoint somebody else runs. Tools
always travel over MCP; a UI is an optional contribution. See
``docs/EXTENSIBILITY.md`` for the contract and ``docs/BUILD_PLAN.md``
for what is built and in what order.
"""

from ember_code.extensions.origin import ExtensionOrigin, ExtensionOrigins

__all__ = ["ExtensionOrigin", "ExtensionOrigins"]
