"""A person's MCP approvals, kept by igni server (its ``docs/service-identity.md`` §14).

In the cloud igni in a person's VM keeps no approvals file: the approvals
are the person's, on igni server, under the desktop's key — a server's
name and the configuration it came from — so approving once holds in
every VM of theirs and survives the VM. Group policy still decides which
servers are offered at all (:class:`CloudMcpApprovals`).
Only people approve; an app's token is refused (403), and an app's VM
then connects nothing that needs approving.

Read with the person's session, cached for :attr:`PersonMcpApprovals.TTL_SECONDS`
(connecting servers asks once per server, at once); a server that cannot
be reached approves nothing — "could not ask" never means "yes".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from ember_code.core.activity.http import busy_call
from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.config.group_policy import group_policy_dir
from ember_code.core.config.settings import load_settings
from ember_code.core.mcp.approval import McpApprovalPolicy

logger = logging.getLogger(__name__)


class McpApproval(BaseModel):
    server: str
    source: str
    approved_at: str | None = None


class McpApprovalList(BaseModel):
    approvals: list[McpApproval] = Field(default_factory=list)

    def keys(self) -> set[tuple[str, str]]:
        return {(a.server, a.source) for a in self.approvals}


class PersonMcpApprovals:
    """``GET`` / ``POST`` / ``DELETE /v1/me/mcp-approvals`` with the person's session."""

    PATH: ClassVar[str] = "/v1/me/mcp-approvals"
    TTL_SECONDS: ClassVar[float] = 60.0
    TIMEOUT_SECONDS: ClassVar[float] = 10.0

    def __init__(
        self,
        *,
        api_url: Callable[[], str | None],
        token: Callable[[], str | None],
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_url = api_url
        self._token = token
        self._transport = transport
        self._clock = clock
        self._known: set[tuple[str, str]] | None = None
        self._read_at = 0.0

    def approved(self, server: str, source: str) -> bool:
        if self._known is None or self._clock() - self._read_at >= self.TTL_SECONDS:
            self._refresh()
        return (server, source) in (self._known or set())

    def approve(self, server: str, source: str) -> bool:
        """Record the approval on igni server; ``True`` when it holds."""
        answered = self._call("POST", json={"server": server, "source": source})
        if answered is not None:
            self._remember(answered)
        return (server, source) in (self._known or set())

    def withdraw(self, server: str, source: str) -> None:
        withdrawn = self._call(
            "DELETE", params={"server": server, "source": source}, expect_body=False
        )
        if withdrawn is not None and self._known is not None:
            self._known.discard((server, source))

    def forget(self) -> None:
        """Read them again on the next ask (a sign-in, a restore)."""
        self._known = None

    def _refresh(self) -> None:
        answered = self._call("GET")
        if answered is None:
            # Unreachable or refused: approve nothing until it answers.
            self._known = set()
            self._read_at = self._clock()
            return
        self._remember(answered)

    def _remember(self, listing: McpApprovalList) -> None:
        self._known = listing.keys()
        self._read_at = self._clock()

    def _call(
        self,
        method: str,
        *,
        json: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        expect_body: bool = True,
    ) -> McpApprovalList | None:
        api_url, token = self._api_url(), self._token()
        if not api_url or not token:
            logger.info("MCP approvals: nobody is signed in to igni server")
            return None
        try:
            with (
                busy_call("igni-server", "MCP approvals"),
                httpx.Client(timeout=self.TIMEOUT_SECONDS, transport=self._transport) as client,
            ):
                response = client.request(
                    method,
                    f"{api_url.rstrip('/')}{self.PATH}",
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            logger.warning("MCP approvals: igni server could not be reached: %s", exc)
            return None
        if response.status_code >= 300:
            logger.warning("MCP approvals: igni server answered %s", response.status_code)
            return None
        if not expect_body:
            return McpApprovalList()
        try:
            return McpApprovalList.model_validate_json(response.content)
        except ValidationError as exc:
            logger.warning("MCP approvals: igni server's answer is malformed: %s", exc)
            return None


class ConfiguredApiUrl:
    """igni server's URL from this process's settings, read once."""

    def __init__(self) -> None:
        self._url: str | None = None
        self._read = False

    def __call__(self) -> str | None:
        if not self._read:
            self._read = True
            try:
                self._url = load_settings().api_url or None
            except Exception as exc:
                logger.warning("MCP approvals: settings did not load: %s", exc)
        return self._url


class SessionToken:
    """The person's session token, read fresh (it is renewed)."""

    def __call__(self) -> str | None:
        return CloudCredentials().access_token


class CloudMcpApprovals(McpApprovalPolicy):
    """A server is approved when group policy offers it and the person approved it.

    Offered: igni server delivered it as a group policy's ``mcps`` entry,
    which an administrator configured. Approved: the person's approval,
    kept on igni server (:class:`PersonMcpApprovals`) — once, for every VM
    of theirs. :meth:`approve` records it there; nothing not offered can
    be approved, and nobody is prompted in a VM.
    """

    def __init__(
        self,
        *,
        delivered_dir: Path | None = None,
        person: PersonMcpApprovals | None = None,
    ) -> None:
        #: Where igni server's group policy delivers MCP configs.
        self._delivered = delivered_dir or (group_policy_dir() / "mcps")
        self._person = (
            person
            if person is not None
            else PersonMcpApprovals(api_url=ConfiguredApiUrl(), token=SessionToken())
        )

    def offered(self, config_path: str) -> bool:
        try:
            return Path(config_path).resolve().is_relative_to(self._delivered.resolve())
        except (OSError, ValueError):
            return False

    def is_approved(self, server_name: str, config_path: str) -> bool:
        return self.offered(config_path) and self._person.approved(server_name, config_path)

    def approve(self, server_name: str, config_path: str) -> None:
        if not self.offered(config_path):
            logger.warning(
                "MCP '%s' from %s is not offered by group policy; not approving it",
                server_name,
                config_path,
            )
            return
        self._person.approve(server_name, config_path)

    def refusal(self, server_name: str, config_path: str) -> str:
        return (
            f"MCP '{server_name}' from {config_path} is not offered by group policy and "
            "approved by the person on igni server (/v1/me/mcp-approvals) — not connecting."
        )


__all__ = [
    "CloudMcpApprovals",
    "ConfiguredApiUrl",
    "McpApproval",
    "McpApprovalList",
    "PersonMcpApprovals",
    "SessionToken",
]
