"""The extension system.

An extension is one concept with two packagings — a signed sidecar
image we supervise, or a remote endpoint somebody else runs. Tools
always travel over MCP; a UI is an optional contribution. See
``docs/EXTENSIBILITY.md`` for the contract and ``docs/BUILD_PLAN.md``
for what is built and in what order.
"""

from ember_code.extensions.manifest import ExtensionManifest
from ember_code.extensions.origin import ExtensionOrigin, ExtensionOrigins
from ember_code.extensions.profile import DeploymentProfile
from ember_code.extensions.registry import (
    ExtensionMethod,
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)
from ember_code.extensions.rpc import ExtensionRpc
from ember_code.extensions.service import (
    ExtensionService,
    ExtensionServiceError,
    McpExtensionService,
)

__all__ = [
    "ExtensionManifest",
    "ExtensionMethod",
    "ExtensionOrigin",
    "ExtensionOrigins",
    "DeploymentProfile",
    "ExtensionRefused",
    "ExtensionRegistry",
    "ExtensionRpc",
    "ExtensionService",
    "ExtensionServiceError",
    "ExtensionState",
    "InstalledExtension",
    "McpExtensionService",
]
