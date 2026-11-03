"""`FakeIgni`'s VM profiles: requests, an org admin's decisions, and what a VM gets.

What `FakeIgni.vm_profiles` keeps, with igni server's rules
(`docs/service-identity.md` §12):

* an **app** requests with its own token, for itself only; the same
  request again keeps its state, a changed one is `pending` again;
* an **extension**'s request is its uploaded manifest's `vm` section, one
  per organisation, appearing the first time anything asks;
* a request over the platform's caps is refused (400);
* nothing changes what a VM gets until a test, playing the org admin,
  calls `approve` — which makes the profile (or replaces the one an
  earlier approval made) and, for an app, assigns it — or `reject`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError, model_validator

from igni_service.vm_profiles import (
    Assigned,
    Decider,
    EffectiveVmProfile,
    RequestedVmProfile,
    Requester,
    RequestSubject,
    VmPackages,
    VmProfileRequest,
    VmProfileRequestRecord,
)

#: The deployment's Standard profile, as igni server ships it.
STANDARD: dict[str, Any] = {
    "memory_mib": 4096,
    "normal_memory_mib": 1024,
    "expected_memory_mib": None,
    "vcpus": 1,
    "idle_before_pause_s": 10,
    "packages": {"apt": [], "pip": [], "npm": []},
    "setup_script": None,
    "volume_gib": 10,
    "volume_max_gib": 50,
    "erase_after_idle_days": None,
    "internet": False,
}

#: The manifest's spelling of each field (camelCase, as the rest of it).
MANIFEST_FIELDS: dict[str, str] = {
    "memoryMib": "memory_mib",
    "normalMemoryMib": "normal_memory_mib",
    "expectedMemoryMib": "expected_memory_mib",
    "vcpus": "vcpus",
    "idleBeforePauseS": "idle_before_pause_s",
    "packages": "packages",
    "setupScript": "setup_script",
    "volumeGib": "volume_gib",
    "volumeMaxGib": "volume_max_gib",
    "eraseAfterIdleDays": "erase_after_idle_days",
    "internet": "internet",
}


class FakeCaps(BaseModel):
    """The platform's caps; igni server's defaults."""

    max_memory_mib: int = 8192
    max_vcpus: int = 4
    max_idle_before_pause_s: int | None = 3600
    max_volume_gib: int = 200


class Resolved(RequestedVmProfile):
    """A request with the Standard values filled in, checked as igni checks a profile."""

    @model_validator(mode="after")
    def _fits(self) -> Resolved:
        if self.normal_memory_mib > self.memory_mib:
            raise ValueError("normal_memory_mib may not exceed memory_mib")
        if self.expected_memory_mib is not None and self.expected_memory_mib > self.memory_mib:
            raise ValueError("expected_memory_mib may not exceed memory_mib")
        if self.volume_gib > self.volume_max_gib:
            raise ValueError("volume_gib may not exceed volume_max_gib")
        return self


class FakeProfile(BaseModel):
    id: str
    name: str
    settings: Resolved


class FakeVmProfiles:
    """One deployment's VM profiles and the requests for them, per organisation."""

    def __init__(self) -> None:
        self.caps = FakeCaps()
        #: Every request, by `(org_id, kind, subject_id)`.
        self.requests: dict[tuple[str, str, str], VmProfileRequestRecord] = {}
        #: Profiles approvals made, by `(org_id, profile_id)`.
        self.profiles: dict[tuple[str, str], FakeProfile] = {}
        #: The profile each app has, by `(org_id, app_id)`; none: the default.
        self.app_profiles: dict[tuple[str, str], str] = {}
        #: How many groups and apps a profile was given, by `(org_id, profile_id)`.
        self.assigned: dict[tuple[str, str], Assigned] = {}

    # ── Playing the org admin ───────────────────────────────────────

    def request(self, org_id: str, kind: str, subject_id: str) -> VmProfileRequestRecord:
        return self.requests[(org_id, kind, subject_id)]

    def approve(
        self, org_id: str, kind: str, subject_id: str, *, by: str = "u-admin"
    ) -> VmProfileRequestRecord:
        """Approve it, as an org admin: the profile is made or replaced, an app's assigned."""
        record = self.request(org_id, kind, subject_id)
        profile_id = record.profile_id or str(uuid.uuid4())
        self.profiles[(org_id, profile_id)] = FakeProfile(
            id=profile_id,
            name=record.subject.name or subject_id,
            settings=Resolved.model_validate(record.requested.model_dump()),
        )
        if kind == "app":
            self.app_profiles[(org_id, subject_id)] = profile_id
        return self._decide(record, "approved", by, profile_id=profile_id)

    def reject(
        self,
        org_id: str,
        kind: str,
        subject_id: str,
        *,
        reason: str | None = None,
        by: str = "u-admin",
    ) -> VmProfileRequestRecord:
        """Reject it, as an org admin: what the VM gets is untouched."""
        record = self.request(org_id, kind, subject_id)
        return self._decide(record, "rejected", by, reason=reason)

    def assign(self, org_id: str, profile_id: str, *, groups: int = 0, apps: int = 0) -> None:
        """Say how many groups and apps an org admin gave a profile."""
        self.assigned[(org_id, profile_id)] = Assigned(groups=groups, apps=apps)

    def _decide(
        self,
        record: VmProfileRequestRecord,
        state: str,
        by: str,
        *,
        profile_id: str | None = None,
        reason: str | None = None,
    ) -> VmProfileRequestRecord:
        decided = record.model_copy(
            update={
                "state": state,
                "decided_by": Decider(kind="person", id=by, acting_as="org_admin"),
                "decided_at": datetime.now(UTC),
                "reason": reason,
                "profile_id": profile_id or record.profile_id,
            }
        )
        self.requests[(record.org_id, record.subject.kind, record.subject.id)] = decided
        return decided

    # ── The wire ────────────────────────────────────────────────────

    def app_put(self, org_id: str, app_id: str, name: str, body: bytes) -> httpx.Response:
        try:
            wanted = VmProfileRequest.model_validate_json(body or b"{}")
            resolved = self._resolve(wanted.body())
        except (ValidationError, ValueError) as exc:
            return httpx.Response(422, json={"detail": str(exc)})
        over = self._over_caps(resolved)
        if over:
            return httpx.Response(400, json={"detail": over})
        record, changed = self._file(
            org_id, "app", app_id, name, resolved, requested_by={"kind": "app", "id": app_id}
        )
        return httpx.Response(201 if changed else 200, json=record.model_dump(mode="json"))

    def app_get(self, org_id: str, app_id: str) -> httpx.Response:
        record = self.requests.get((org_id, "app", app_id))
        if record is None:
            return httpx.Response(404, json={"detail": "This app has asked for no VM profile"})
        return httpx.Response(200, json=record.model_dump(mode="json"))

    def app_profile(self, org_id: str, app_id: str) -> httpx.Response:
        profile = self._profile(org_id, self.app_profiles.get((org_id, app_id)))
        served = self._served(profile)
        return httpx.Response(
            200, json={**served.model_dump(mode="json"), "volume_gib": profile.settings.volume_gib}
        )

    def service(
        self, org_id: str, client_id: str, extension_id: str | None, manifest: dict[str, Any] | None
    ) -> httpx.Response:
        """What `GET /v1/service-vm-profile` answers a service for one organisation."""
        vm = (manifest or {}).get("vm")
        if extension_id is None or not isinstance(vm, dict):
            return httpx.Response(200, json={"org_id": org_id, "extension_id": None})
        try:
            resolved = self._resolve(
                {MANIFEST_FIELDS[key]: value for key, value in vm.items() if key in MANIFEST_FIELDS}
            )
        except (ValidationError, ValueError) as exc:
            return httpx.Response(422, json={"detail": str(exc)})
        record, _ = self._file(
            org_id,
            "extension",
            extension_id,
            str((manifest or {}).get("name") or extension_id),
            resolved,
            requested_by={"kind": "service", "id": client_id},
        )
        profile = self.profiles.get((org_id, record.profile_id)) if record.profile_id else None
        return httpx.Response(
            200,
            json={
                "org_id": org_id,
                "extension_id": extension_id,
                "request": record.model_dump(mode="json"),
                "profile": self._served(profile).model_dump(mode="json") if profile else None,
                "assigned": self.assigned.get(
                    (org_id, profile.id) if profile else ("", ""), Assigned()
                ).model_dump(),
            },
        )

    # ── igni's rules ────────────────────────────────────────────────

    @staticmethod
    def _resolve(fields: dict[str, Any]) -> Resolved:
        return Resolved.model_validate({**STANDARD, **fields})

    def _over_caps(self, wanted: Resolved) -> str | None:
        caps = self.caps
        if wanted.memory_mib > caps.max_memory_mib:
            return f"The memory ceiling may be at most {caps.max_memory_mib} MiB on this platform"
        if wanted.vcpus > caps.max_vcpus:
            return f"At most {caps.max_vcpus} vCPUs on this platform"
        longest = caps.max_idle_before_pause_s
        if longest is not None and (
            wanted.idle_before_pause_s is None or wanted.idle_before_pause_s > longest
        ):
            return f"A VM may stay awake idle for at most {longest} seconds on this platform"
        if wanted.volume_max_gib > caps.max_volume_gib:
            return f"A volume may grow to at most {caps.max_volume_gib} GiB on this platform"
        return None

    @staticmethod
    def _digest(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:32]

    def _file(
        self,
        org_id: str,
        kind: str,
        subject_id: str,
        name: str,
        resolved: Resolved,
        *,
        requested_by: dict[str, str],
    ) -> tuple[VmProfileRequestRecord, bool]:
        """Store the request; whether it is new or changed (and so pending)."""
        values = resolved.model_dump(mode="json")
        version = self._digest(values)
        held = self.requests.get((org_id, kind, subject_id))
        if held is not None and held.version == version:
            return held, False
        record = VmProfileRequestRecord(
            id=held.id if held else str(uuid.uuid4()),
            org_id=org_id,
            subject=RequestSubject.model_validate({"kind": kind, "id": subject_id, "name": name}),
            state="pending",
            requested=RequestedVmProfile.model_validate(values),
            version=version,
            packages_version=self._digest(
                {"packages": values["packages"], "setup_script": values["setup_script"]}
            ),
            within_caps=self._over_caps(resolved) is None,
            requested_by=Requester.model_validate(requested_by),
            requested_at=datetime.now(UTC),
            profile_id=held.profile_id if held else None,
        )
        self.requests[(org_id, kind, subject_id)] = record
        return record, True

    def _profile(self, org_id: str, profile_id: str | None) -> FakeProfile:
        found = self.profiles.get((org_id, profile_id)) if profile_id else None
        return found or FakeProfile(
            id=f"default-{org_id}", name="Standard", settings=self._resolve({})
        )

    def _served(self, profile: FakeProfile) -> EffectiveVmProfile:
        s = profile.settings
        caps = self.caps
        memory = min(s.memory_mib, caps.max_memory_mib)
        idle = s.idle_before_pause_s
        if caps.max_idle_before_pause_s is not None:
            idle = (
                caps.max_idle_before_pause_s
                if idle is None
                else min(idle, caps.max_idle_before_pause_s)
            )
        return EffectiveVmProfile(
            profile_id=profile.id,
            profile_name=profile.name,
            memory_mib=memory,
            normal_memory_mib=min(s.normal_memory_mib, memory),
            expected_memory_mib=None
            if s.expected_memory_mib is None
            else min(s.expected_memory_mib, memory),
            vcpus=min(s.vcpus, caps.max_vcpus),
            idle_before_pause_s=idle,
            packages=VmPackages.model_validate(s.packages.model_dump()),
            setup_script=s.setup_script,
            volume_max_gib=min(s.volume_max_gib, caps.max_volume_gib),
            erase_after_idle_days=s.erase_after_idle_days,
            internet=s.internet,
        )


__all__ = ["FakeCaps", "FakeVmProfiles"]
