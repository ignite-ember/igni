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


class HostError(IgniExtensionError):
    """The igni host refused a request on `ext.host`, or it failed there.

    `code` says which, for code to branch on: `not_granted` (the manifest's
    `permissions.host` lacks the object), `not_found`, `invalid`,
    `read_only`, `stage_2` (defined, not yet available), `refused` (the
    person could not have done it either), `failed`. The message is a
    sentence for the extension's author.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
