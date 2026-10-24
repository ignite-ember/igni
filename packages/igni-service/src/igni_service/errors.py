"""What can go wrong, by who has to act on it.

`IgniError` is the root, so a service that wants one `except` has one.
Below it the split is about what to do next:

* `IgniUnavailable` — igni could not be reached, or answered with
  something else. Try again later; nothing about the request was wrong.
* `NotificationRefused` — igni read the notifications and said no. Trying
  again sends the same refusal.
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


class NotificationRefused(IgniError):
    """igni read the notifications and refused them. Sending them again will not help."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"igni refused the notifications: {status} {detail}")
        self.status = status
        self.detail = detail


class KeyStoreError(IgniError):
    """This service's key cannot be stored or read."""


class BundleError(IgniError):
    """The extension bundle is unreadable, or could not be built."""


class ConfigurationError(IgniError):
    """The service is missing something it needs to be part of igni."""
