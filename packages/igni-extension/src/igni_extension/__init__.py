"""igni-extension: an extension's process, as its author writes it.

    ext = IgniExtension("acme.example")

    @ext.tool
    async def do_thing() -> str: ...

    ext.run()

See the package README for the whole surface.
"""

from igni_extension.environment import (
    ExtensionEnvironment,
    HostEndpoint,
    Secrets,
    Workspace,
    WorkspaceMode,
)
from igni_extension.errors import (
    HostError,
    HostUnavailable,
    IgniExtensionError,
    NotConfigured,
    NotSignedIn,
)
from igni_extension.extension import IgniExtension
from igni_extension.host import Host, Notifications
from igni_extension.host_model import SCHEMA_VERSION
from igni_extension.members import Property, Signal
from igni_extension.notifications import LocalNotification, Target
from igni_extension.prompt import PromptConditions
from igni_extension.service import ServiceClient
from igni_extension.tokens import SignedIn

__all__ = [
    "ExtensionEnvironment",
    "Host",
    "HostEndpoint",
    "HostError",
    "HostUnavailable",
    "IgniExtension",
    "IgniExtensionError",
    "LocalNotification",
    "NotConfigured",
    "NotSignedIn",
    "Notifications",
    "PromptConditions",
    "Property",
    "SCHEMA_VERSION",
    "Secrets",
    "ServiceClient",
    "Signal",
    "SignedIn",
    "Target",
    "Workspace",
    "WorkspaceMode",
]
