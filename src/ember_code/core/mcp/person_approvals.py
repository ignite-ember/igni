"""A person's MCP approvals, kept by igni server (its ``docs/service-identity.md`` §14).

In the cloud igni in a person's VM keeps no approvals file: the approvals
are the person's, on igni server, under the desktop's key — a server's
name and the configuration it came from — so approving once holds in
every VM of theirs and survives the VM. Group policy still decides which
servers are offered at all (:class:`~ember_code.core.mcp.approval.MCPApprovalManager`).
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
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from ember_code.core.activity.http import busy_call

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


__all__ = ["McpApproval", "McpApprovalList", "PersonMcpApprovals"]
