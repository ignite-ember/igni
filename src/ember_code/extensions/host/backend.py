"""What the host model needs from igni's backend, and nothing more.

The model is implemented *against the existing backend*: a member that
reads or writes something calls the RPC handler, the UI action or the
service that igni's own UI uses, so the model never holds a second copy
of igni's logic — and a write the person could not make is refused by
the same check that refuses the person.

This protocol is the seam. :class:`~ember_code.backend.host_backend.OrchestratorHostBackend`
implements it over the session pool; the model's tests drive that same
class over a pool of stand-in sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ember_code.protocol.messages import Message
from ember_code.protocol.rpc import RpcMethod


class HostBackend(Protocol):
    """igni's backend, as the host model reaches it."""

    #: The project this backend was started in.
    project_dir: Path

    #: :class:`~ember_code.core.session.client_state.ClientStateStore`.
    client_state: Any

    def default_session(self) -> str:
        """The boot session — what a view that binds nothing talks to."""
        ...

    def live_sessions(self) -> list[str]:
        """Sessions with a runtime now."""
        ...

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        """Call an RPC handler in a session, as the UI would. JSON back.

        Raises whatever the handler raised: the model turns it into a
        refusal or a failure.
        """
        ...

    async def act(self, message: Message, *, session_id: str) -> None:
        """Send a UI action into a session, as a window would.

        Returns once it is accepted; whatever it streams goes to every
        window, as the person's own action's would.
        """
        ...

    async def ask(self, message: Message, *, session_id: str, timeout: float = 120.0) -> Message:
        """Send a UI action whose answer comes back here, not to the windows."""
        ...

    async def attach(self, session_id: str, project_dir: str | None) -> dict[str, str]:
        """``attach_session``: make a session live, refused where the person's would be."""
        ...

    async def post(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: dict[str, str] | None,
        queued: bool,
    ) -> None:
        """A message in the chat, labelled as ``extension_id``, for the agent."""
        ...

    async def show_view(self, *, session_id: str, payload: dict[str, Any], run_id: str) -> None:
        """Put a json-render block in a session's transcript, and keep it."""
        ...

    def views(self, session_id: str) -> list[dict[str, Any]]:
        """Every json-render block a session's transcript holds."""
        ...

    def project_dir_of(self, session_id: str) -> Path: ...

    def waiting_approvals(self, session_id: str) -> list[Message]:
        """The ``RunPaused`` of every approval the session has waiting —
        what the host knows after a restart, before any window asked."""
        ...

    def broadcast(self, message: Message) -> None:
        """Send to every window, now (scheduled on the loop)."""
        ...


__all__ = ["HostBackend"]
