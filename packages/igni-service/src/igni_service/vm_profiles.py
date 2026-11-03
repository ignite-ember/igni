"""What a producer's VM needs in igni's cloud, asked for — and what it gets.

In igni's cloud every producer runs in a microVM of its own, built from a
**VM profile**: its memory and vCPUs, how long it idles before a pause,
its volume, and the packages and setup script baked into its read-only
layer. An org admin defines profiles and gives one to each group and app
(igni server's `docs/service-identity.md` §12).

Packages and the setup script run **as root** when that layer is built, so
nothing gets into a VM that an org admin has not approved. An app or an
extension that needs something — a headless browser, a native library,
more memory — **requests** it:

* an **app**, as itself (`igni_service.app.IgniApp.request_vm_profile`);
* an **extension**, in its manifest's `vm` section — and the service that
  ships it reads what became of that in each organisation
  (`Host.vm_profile`).

igni keeps the request **pending** and shows the org admin exactly what
would run. Only their approval makes it a profile (an app's is assigned
to the app at once; an extension's the admin gives to the groups and apps
that use it). A changed request is pending again; the profile already
running is untouched until then. Nothing in this SDK can approve: that
takes an org admin's portal session, which no service or app holds.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from igni_service.errors import IgniUnavailable, VmProfileRefused
from igni_service.service_tokens import ServiceTokens
from igni_service.urls import IgniPaths

#: One package as its manager names it — `chromium`, `requests==2.32.3`,
#: `@playwright/mcp@0.0.41` — and nothing a shell would read as more than
#: one word. igni server's own rule, mirrored so a mistake is caught here.
PackageName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9@][A-Za-z0-9@/._+:=<>~^*!,-]{0,199}$", strip_whitespace=True
    ),
]

#: At most this many packages per manager.
MAX_PACKAGES = 200
#: The longest setup script, in characters.
MAX_SETUP_SCRIPT = 65536

RequestState = Literal["pending", "approved", "rejected"]


class VmPackages(BaseModel):
    """What the profile's read-only layer is built with, per package manager."""

    model_config = ConfigDict(extra="forbid")

    apt: list[PackageName] = Field(default_factory=list, max_length=MAX_PACKAGES)
    pip: list[PackageName] = Field(default_factory=list, max_length=MAX_PACKAGES)
    npm: list[PackageName] = Field(default_factory=list, max_length=MAX_PACKAGES)


class VmProfileRequest(BaseModel):
    """What an app asks its VM to have. Every field is optional.

    A field left out takes the deployment's Standard value, decided by igni
    server — only the fields set are sent (`body`). A nullable field set to
    `None` asks for that explicitly: `idle_before_pause_s=None` is never
    pausing, which the platform's caps may refuse.
    """

    model_config = ConfigDict(extra="forbid")

    #: The most memory the VM may have, in MiB: its size at boot.
    memory_mib: int | None = Field(default=None, ge=128, le=1_048_576)
    #: What it normally has, in MiB; the node lends the rest while it needs it.
    normal_memory_mib: int | None = Field(default=None, ge=128, le=1_048_576)
    #: What the node expects it to use, for placing it; `None`: the node's rule.
    expected_memory_mib: int | None = Field(default=None, ge=128, le=1_048_576)
    vcpus: int | None = Field(default=None, ge=1, le=256)
    #: Seconds idle before the VM is paused; `None` is never.
    idle_before_pause_s: int | None = Field(default=None, ge=0)
    packages: VmPackages | None = None
    #: Run as root when the profile's layer is built, after the packages.
    setup_script: str | None = Field(default=None, max_length=MAX_SETUP_SCRIPT)
    #: A new VM's volume, and how far an admin may grow it, in GiB.
    volume_gib: int | None = Field(default=None, ge=1)
    volume_max_gib: int | None = Field(default=None, ge=1)
    #: Days unwoken after which the app's VM is erased; `None` is never.
    erase_after_idle_days: int | None = Field(default=None, ge=1, le=3650)
    #: Whether the VM may reach the internet. Left out (or False): internal
    #: only — igni and the deployment's own allowed addresses. True asks the
    #: org admin for internet egress, a security grant they see as such;
    #: a node that is air-gapped, or not allowed to grant internet, still
    #: starts the VM internal-only.
    internet: bool | None = None

    def body(self) -> dict[str, object]:
        """The JSON igni is sent: only what was set."""
        return self.model_dump(mode="json", exclude_unset=True)


class RequestedVmProfile(BaseModel):
    """A request as igni resolved it: every value, the Standard ones filled in — what would run."""

    memory_mib: int
    normal_memory_mib: int
    expected_memory_mib: int | None = None
    vcpus: int
    idle_before_pause_s: int | None = None
    packages: VmPackages = Field(default_factory=VmPackages)
    setup_script: str | None = None
    volume_gib: int
    volume_max_gib: int
    erase_after_idle_days: int | None = None
    #: Internet egress asked for; False is internal only.
    internet: bool = False


class RequestSubject(BaseModel):
    """Whose request it is: an app, or an extension."""

    kind: Literal["app", "extension"]
    id: str
    name: str = ""


class Requester(BaseModel):
    kind: str
    id: str


class Decider(BaseModel):
    """The admin who approved or rejected it."""

    kind: str
    id: str
    #: `org_admin` or `super_admin`.
    acting_as: str


class VmProfileRequestRecord(BaseModel):
    """One request, and what became of it."""

    id: str
    org_id: str
    subject: RequestSubject
    state: RequestState
    requested: RequestedVmProfile
    #: Changes whenever anything requested does; an approval names it.
    version: str
    #: Changes whenever the packages or the setup script do: the layer Fleet builds.
    packages_version: str
    #: False when it asks for more than the platform's caps allow now.
    within_caps: bool
    requested_by: Requester
    requested_at: datetime
    decided_by: Decider | None = None
    decided_at: datetime | None = None
    #: Why it was rejected, when the admin said.
    reason: str | None = None
    #: The profile an approval made, holding what was last approved.
    profile_id: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.state == "pending"

    @property
    def is_approved(self) -> bool:
        return self.state == "approved"

    @property
    def is_rejected(self) -> bool:
        return self.state == "rejected"


class EffectiveVmProfile(BaseModel):
    """What a VM gets from a profile: its values, clamped to the platform's caps."""

    profile_id: str
    profile_name: str
    memory_mib: int
    normal_memory_mib: int
    expected_memory_mib: int | None = None
    vcpus: int
    idle_before_pause_s: int | None = None
    packages: VmPackages = Field(default_factory=VmPackages)
    setup_script: str | None = None
    volume_max_gib: int
    erase_after_idle_days: int | None = None
    #: Whether the VM may reach the internet (where its node may grant it);
    #: False, and absent from an older igni server: internal only.
    internet: bool = False


class AppVmProfile(EffectiveVmProfile):
    """What the app's own VM gets now, with its volume."""

    #: The app's volume, in GiB: the profile's, or more where an admin grew it.
    volume_gib: int


class Assigned(BaseModel):
    """How many of the organisation's groups and apps have a profile."""

    groups: int = 0
    apps: int = 0


class ServiceVmProfile(BaseModel):
    """A service's extension's request in one organisation, and the profile it became."""

    org_id: str
    #: `None` when this service ships no extension, or its manifest has no `vm`.
    extension_id: str | None = None
    request: VmProfileRequestRecord | None = None
    #: What the approval made, clamped to the caps; `None` until approved.
    profile: EffectiveVmProfile | None = None
    #: Who the org admin gave that profile to.
    assigned: Assigned = Field(default_factory=Assigned)


def refusal(response: httpx.Response) -> VmProfileRefused:
    """igni's own words for a refusal: its `detail`, or the body."""
    detail: object = response.text
    with contextlib.suppress(ValueError, AttributeError):
        detail = response.json().get("detail", detail)
    return VmProfileRefused(response.status_code, str(detail))


class ServiceVmProfiles:
    """`GET /v1/service-vm-profile`, with this service's token for one organisation."""

    def __init__(self, tokens: ServiceTokens) -> None:
        self._tokens = tokens

    async def of(self, org_id: str) -> ServiceVmProfile:
        """Raises `IgniUnavailable`."""
        response = await self._tokens.request("GET", IgniPaths.SERVICE_VM_PROFILE, org_id=org_id)
        if response.status_code != 200:
            raise IgniUnavailable(f"igni answered the VM profile read {response.status_code}")
        try:
            return ServiceVmProfile.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise IgniUnavailable("igni answered the VM profile read with something else") from exc
