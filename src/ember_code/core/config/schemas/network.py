"""``network`` config — which hosts are inside the customer's network.

Read only under ``air_gapped: true``, by
:class:`~ember_code.core.network_policy.NetworkPolicy`: a connection igni
starts itself (an ``igni ext install`` clone, an MCP server reached by
URL, an ``http`` hook, a plugin or marketplace clone) goes to a local
path, a loopback address, or a host listed here — or it is refused
before it is attempted.

The hosts of ``api_url``, ``portal_url``, ``update_endpoint`` and
``plugins.marketplaces`` count as inside without being repeated here.

A project's ``.igni/config.yaml`` cannot set this block: a repository
somebody cloned must not be able to open the air gap it runs under. It
comes from the user's ``~/.igni/config.yaml``, the managed-settings file
or the organisation's group policy.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NetworkConfig(BaseModel):
    #: Hosts inside the network: an exact name (``git.acme.internal``,
    #: ``10.0.4.12``) or a suffix pattern (``*.acme.internal``, which
    #: covers ``git.acme.internal`` but not ``acme.internal`` itself).
    internal_hosts: list[str] = Field(default_factory=list)
