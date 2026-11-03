"""What can go wrong, by who has to act on it.

`IgniError` is the root, so a service that wants one `except` has one.
Below it the split is about what to do next:

* `IgniUnavailable` — igni could not be reached, or answered with
  something else. Try again later; nothing about the request was wrong.
  Two of its kinds name what a person has to do first:
  `ServiceNotApproved` (a super admin has not approved this service in
  igni's portal yet) and `ServiceRevoked` (one revoked it). Both are
  `IgniUnavailable`, so an `except IgniUnavailable` still catches them.
* `NotificationRefused` — igni read the notifications and said no. Trying
  again sends the same refusal.
* `EventRefused` — igni read the event and said no: the producer is not in
  the organisation, does not have the extension on, or the event is invalid.
* `VmProfileRefused` — igni read a VM profile request and said no: over
  the platform's caps, malformed, or for an app that is not the caller.
* `KeyStoreError` — this service cannot keep or read its own key. An
  operator has to fix the configuration.
* `BundleError` — the extension bundle is unreadable, or cannot be built.
* `ConfigurationError` — the service was started without what it needs.
"""

from __future__ import annotations


class IgniError(Exception):
    """Anything this SDK raises on purpose."""


class IgniUnavailable(IgniError):
    """igni server could not be reached, refused, or answered with something else."""


class ServiceNotApproved(IgniUnavailable):
    """igni server holds this service's registration, and no super admin has approved it yet.

    A super admin approves it in igni's portal under *Services*, after
    checking the thumbprint the service logged at start-up. Retrying
    before then gets the same answer.
    """


class ServiceRevoked(IgniUnavailable):
    """A super admin revoked this service in igni's portal. Nothing it asks igni for works now."""


class NotificationRefused(IgniError):
    """igni read the notifications and refused them. Sending them again will not help."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"igni refused the notifications: {status} {detail}")
        self.status = status
        self.detail = detail


class EventRefused(IgniError):
    """igni read the event and refused it. Sending it again will not help."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"igni refused the event: {status} {detail}")
        self.status = status
        self.detail = detail


class VmProfileRefused(IgniError):
    """igni read the VM profile request and refused it. Sending it again will not help."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"igni refused the VM profile request: {status} {detail}")
        self.status = status
        self.detail = detail


class KeyStoreError(IgniError):
    """This service's key cannot be stored or read."""


class BundleError(IgniError):
    """The extension bundle is unreadable, or could not be built."""


class ConfigurationError(IgniError):
    """The service is missing something it needs to be part of igni."""
