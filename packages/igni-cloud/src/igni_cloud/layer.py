"""The cloud layer: igni in a producer's microVM (``IGNI_PROFILE=cloud``).

``docs/CLOUD.md`` §9. Declared as the ``cloud`` entry point of the
``igni.layers`` group, so the core's :class:`~ember_code.core.layer.Layers`
installs it when the image sets ``IGNI_PROFILE=cloud`` — for the backend,
``igni migrate`` and the CLI alike. What it changes:

* **logs** — JSON lines on standard output, collected by the node; no log
  file under ``~/.igni`` (:mod:`igni_cloud.logs`);
* **credentials** — the token the VM keeps from its own credential, never
  ``credentials.json`` (:mod:`igni_cloud.credentials`);
* **settings** — only the image's (command line, and the managed policy
  the node agent writes to ``/run/igni/managed-settings.yaml``) and igni
  server's (group policy); nothing under ``~/.igni`` or the workspace;
* **state** — must be in ``IGNI_DATABASE_URL``;
* **the extension listing** — the priority and group igni server sends are
  kept in the process, never written;
* **MCP approvals** — offered by group policy, approved by the person on
  igni server (:mod:`igni_cloud.mcp_approvals`);
* **the platform's calls** — ``deliver_service_events`` and
  ``vm_sign_in`` only on the gateway's control stream; igni pulls no
  events itself;
* **the backend's boot and serving** — the guest contract with the VM's
  agents: ``ready``, late binding to its producer, ``activity.json``,
  restores, the VM's own credential (:mod:`igni_cloud.guest`).

PostgreSQL storage is not part of the layer: it is chosen by the database
URL (:mod:`igni_cloud.storage.postgres`, the ``igni.storage`` entry point
``postgresql``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from ember_code.core.auth.credentials import CredentialSource
from ember_code.core.layer import BackendHooks, Layer
from ember_code.core.mcp.approval import McpApprovalPolicy
from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest.contract import CloudGuest
from igni_cloud.guest.paths import GuestPaths
from igni_cloud.logs import StdoutLog
from igni_cloud.mcp_approvals import CloudMcpApprovals


class CloudLayer(Layer):
    name: ClassVar[str] = "cloud"

    def log_files(self) -> bool:
        return False

    def log_handler(self) -> logging.Handler:
        return StdoutLog.handler()

    def credentials(self) -> CredentialSource | None:
        return EnvironmentCredentials()

    def reads_local_settings(self) -> bool:
        return False

    def requires_database(self) -> bool:
        return True

    def keeps_listing_in_files(self) -> bool:
        return False

    def mcp_approvals(self) -> McpApprovalPolicy | None:
        return CloudMcpApprovals()

    def managed_settings_path(self) -> Path | None:
        # The node agent writes the producer's managed policy into the run
        # directory at every start, after the token and before producer.env.
        return GuestPaths.from_env().managed_settings

    def platform_calls_need_control(self) -> bool:
        # Only the gateway's control stream: the node agent adds the header
        # to it and strips it from every client's.
        return True

    def fetches_own_events(self) -> bool:
        # The gateway pulls them and delivers them on its control stream.
        return False

    def backend(self) -> BackendHooks:
        return CloudGuest.from_env()


__all__ = ["CloudLayer"]
