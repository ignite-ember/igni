"""`ext.host`: igni as an extension sees it, one object per part of the app.

The objects are generated from igni's host schema into
`igni_extension.host_model` — `host.sessions`, `host.chat`, `host.ui`,
`host.workspace` and the rest, each with its properties, signals and
methods. This module names the root and keeps the names this package
had before the model was generated.
"""

from __future__ import annotations

from igni_extension.host_model import Host, HostNotifications

#: `ext.host.notifications`, under the name it had before the model.
Notifications = HostNotifications

__all__ = ["Host", "Notifications"]
