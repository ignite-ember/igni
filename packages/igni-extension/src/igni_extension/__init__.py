"""igni-extension: an extension's process, as its author writes it.

    ext = IgniExtension("acme.example")

    @ext.tool(hints=ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False))
    async def do_thing() -> str: ...

    ext.run()

See the package README for the whole surface.
"""

from igni_extension.caller import Caller, CallerKind, NoCaller
from igni_extension.environment import (
    Assets,
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
from igni_extension.hints import ToolHints, ToolHintsMissing
from igni_extension.hooks import Allow, HookEvent, Hooks, Modify, Reject
from igni_extension.host import Host, Notifications
from igni_extension.host_model import SCHEMA_VERSION
from igni_extension.members import Property, Signal
from igni_extension.notifications import LocalNotification, Target
from igni_extension.prompt import PromptConditions
from igni_extension.service import ServiceClient
from igni_extension.service_events import ServiceEvent
from igni_extension.tokens import SignedIn

__all__ = [
    "Allow",
    "Assets",
    "Caller",
    "CallerKind",
    "ExtensionEnvironment",
    "Host",
    "HostEndpoint",
    "HostError",
    "HookEvent",
    "Hooks",
    "HostUnavailable",
    "IgniExtension",
    "IgniExtensionError",
    "LocalNotification",
    "Modify",
    "NoCaller",
    "NotConfigured",
    "NotSignedIn",
    "Notifications",
    "PromptConditions",
    "Reject",
    "Property",
    "SCHEMA_VERSION",
    "Secrets",
    "ServiceClient",
    "ServiceEvent",
    "Signal",
    "SignedIn",
    "Target",
    "ToolHints",
    "ToolHintsMissing",
    "Workspace",
    "WorkspaceMode",
]
