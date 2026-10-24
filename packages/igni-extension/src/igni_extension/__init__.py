"""igni-extension: an extension's process, as its author writes it.

    ext = IgniExtension("acme.example")

    @ext.tool
    async def do_thing() -> str: ...

    ext.run()

See the package README for the whole surface.
"""

from igni_extension.environment import (
    Capabilities,
    Capability,
    ExtensionEnvironment,
    Secrets,
    Workspace,
    WorkspaceMode,
)
from igni_extension.errors import HostUnavailable, IgniExtensionError, NotConfigured, NotSignedIn
from igni_extension.extension import IgniExtension
from igni_extension.host import Host, Notifications
from igni_extension.notifications import LocalNotification, Target
from igni_extension.signals import Signal
from igni_extension.tokens import SignedIn

__all__ = [
    "Capabilities",
    "Capability",
    "ExtensionEnvironment",
    "Host",
    "HostUnavailable",
    "IgniExtension",
    "IgniExtensionError",
    "LocalNotification",
    "NotConfigured",
    "NotSignedIn",
    "Notifications",
    "Secrets",
    "Signal",
    "SignedIn",
    "Target",
    "Workspace",
    "WorkspaceMode",
]
