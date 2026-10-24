"""How the host says no, or that something went wrong."""

from __future__ import annotations

from ember_code.extensions.host.spec import ErrorCode


class HostModelError(Exception):
    """A refusal or failure, with a code for code and a sentence for people.

    Raised anywhere in the host model's implementation; the channel and
    the bridge turn it into ``{ok: false, error: {code, message}}`` and
    both SDKs raise it again, with the same code, in the extension.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @classmethod
    def not_granted(cls, extension_id: str, what: str, permission: str) -> HostModelError:
        if permission == "workspace":
            fix = "Declare `permissions.workspace: read` in its manifest"
        else:
            fix = f"Add '{permission}' to `permissions.host` in its manifest"
        return cls(
            ErrorCode.NOT_GRANTED,
            f"'{extension_id}' is not granted '{permission}', which {what} needs. "
            f"{fix} and reinstall.",
        )

    @classmethod
    def stage_two(cls, member: str) -> HostModelError:
        return cls(
            ErrorCode.STAGE_2,
            f"'{member}' is defined but not yet available: igni has not built what is "
            "behind it yet (docs/HOST_OBJECT_MODEL.md). Write against it; it will start "
            "answering without a change to your code.",
        )


__all__ = ["HostModelError"]
