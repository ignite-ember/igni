"""igni, as one organisation sees it: what `igni.host(org_id)` answers."""

from __future__ import annotations

import warnings
from datetime import datetime

from igni_service.agent_wire import AgentWire
from igni_service.agents import Agent
from igni_service.directory import Directory, DirectoryClient
from igni_service.events import EventAccepted, Producer
from igni_service.notifications import Delivered, Notification, Notifier
from igni_service.vm_profiles import ServiceVmProfile, ServiceVmProfiles


class Host:
    """The floor a service stands on, for one organisation.

    Cheap to make: it holds no state of its own. Tokens, caches and the
    HTTP client are the service's, shared by every `Host`.
    """

    def __init__(
        self,
        org_id: str,
        *,
        notifier: Notifier,
        directory: DirectoryClient,
        agents: AgentWire | None = None,
        vm_profiles: ServiceVmProfiles | None = None,
    ) -> None:
        if not org_id:
            raise ValueError("a host is for one organisation, and none was named")
        self._org_id = org_id
        self._notifier = notifier
        self._agents = agents
        self._vm_profiles = vm_profiles
        self.directory = Directory(directory, org_id)

    @property
    def org_id(self) -> str:
        return self._org_id

    async def notify(self, *notifications: Notification) -> Delivered:
        """Tell people in this organisation that something happened.

        Any number of notifications, in as few calls as igni accepts.
        Raises `IgniUnavailable` when igni could not take them after
        retrying, and `NotificationRefused` when it read them and said no.
        A caller for whom notifying is best effort catches `IgniError`.
        """
        return await self._notifier.send(self._org_id, notifications)

    async def vm_profile(self) -> ServiceVmProfile:
        """Deprecated: `host.extension_vm_profile()`, which says what it answers."""
        warnings.warn(
            "host.vm_profile() is deprecated; call host.extension_vm_profile()",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.extension_vm_profile()

    async def extension_vm_profile(self) -> ServiceVmProfile:
        """What became of this service's extension's VM request here.

        The request is the extension manifest's `vm` section; this
        organisation's admin approves or rejects it. Answers the request's
        state (`pending`, `approved`, `rejected`, with the admin's reason),
        the profile an approval made and how many groups and apps have it.
        Raises `IgniUnavailable`.
        """
        if self._vm_profiles is None:
            raise RuntimeError("this host was made without the VM profiles' client")
        return await self._vm_profiles.of(self._org_id)

    def agent(self, producer: Producer) -> Agent:
        """`producer`'s agent: send it messages, files and events, open chats (`igni_service.agents`)."""
        if self._agents is None:
            raise RuntimeError("this host was made without the agents' wire")
        return Agent(self._org_id, producer, self._agents)

    async def send_event(
        self,
        producer: Producer,
        type: str,
        payload: dict[str, object] | None = None,
        *,
        event_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> EventAccepted:
        """Wake `producer`'s agent with an event for this service's extension.

        `host.agent(producer).send_event(...)`, kept for the services that
        call it. It reaches this extension's own chat in the producer's
        igni, on their desktop or in their Fleet VM, whether or not anybody
        is watching: it starts a run, or is queued for the run already
        going. Pass an `event_key` to make retries exact. Raises
        `IgniUnavailable` or `EventRefused`.
        """
        return await self.agent(producer).send_event(
            type, payload, event_key=event_key, expires_at=expires_at
        )
