"""What can go wrong, by who has to act on it."""

from __future__ import annotations


class IgniExtensionError(Exception):
    """Anything this SDK raises on purpose."""


class NotSignedIn(IgniExtensionError):
    """Nobody is signed in to igni, so there is no token for the service.

    The person can fix this: signing in to igni is the answer.
    """

    def __init__(self, message: str = "Sign in to igni to reach this extension's service.") -> None:
        super().__init__(message)


class NotConfigured(IgniExtensionError):
    """igni did not start this process with what the call needs.

    Running outside igni, or an extension that belongs to no service.
    """


class HostUnavailable(IgniExtensionError):
    """The igni host that started this process did not answer as expected."""
