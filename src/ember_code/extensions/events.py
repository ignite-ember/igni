"""The four host events that predate the host object model.

Before the model, an extension heard four things igni knows — the
session changed, HEAD moved, files changed, an extension's health moved
— as topics granted by ``permissions.events``. Each is now a signal of
the model (``docs/HOST_OBJECT_MODEL.md``, *The existing events*),
granted by ``permissions.host`` like every other, and the topic strings
stay as those signals' **wire names**, so an SDK that knew them still
names the same thing.

The rule the old delivery was built on is the rule the model keeps
(:mod:`ember_code.extensions.host.signals`): a grant is checked where a
signal is dispatched, never where it is received, because a host that
broadcast everything and asked each extension to discard what was not
its own would be trusting the untrusted party to discard it.
"""

from __future__ import annotations

from enum import StrEnum


class HostTopic(StrEnum):
    """The wire names of the four signals that predate the model.

    A ``StrEnum`` for the reason :class:`~ember_code.protocol.rpc.RpcMethod`
    is one: the value *is* what travels, so an emitter writing
    ``HostTopic.WORKSPACE_HEAD_CHANGED`` fails at import when it is
    renamed rather than silently delivering to nobody.
    """

    #: ``sessions.current_changed``
    SESSION_SWITCHED = "session.switched"
    #: ``workspace.head_changed``
    WORKSPACE_HEAD_CHANGED = "workspace.head-changed"
    #: ``workspace.files_changed``
    WORKSPACE_FILES_CHANGED = "workspace.files-changed"
    #: ``extensions.state_changed``
    EXTENSION_STATE_CHANGED = "extension.state-changed"


__all__ = ["HostTopic"]
