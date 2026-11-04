"""Whether a URL igni is about to reach is inside the customer's network.

One answer for every connection igni starts itself: a marketplace or
plugin clone (:class:`~ember_code.core.plugins.source_policy.PluginSourcePolicy`),
``igni ext install`` from a repository, an MCP server reached by URL,
and an ``http`` hook. Each asks :meth:`NetworkPolicy.allows` before it
opens a socket or starts ``git``, so a refused target is never attempted.

On an ordinary install every URL is allowed. Under ``air_gapped: true``
a URL is inside the network when it is:

* **local** — a filesystem path or a ``file://`` URL, which is read
  without a network at all;
* on a **loopback** host — ``localhost``, ``127.0.0.0/8`` or ``::1``,
  which cannot leave the machine;
* on a host the **customer named** — ``network.internal_hosts`` (an
  exact host, or ``*.acme.internal`` for any host under that suffix),
  the hosts of ``api_url``, ``portal_url`` and ``update_endpoint``,
  and the hosts of ``plugins.marketplaces``. Configuring igni to use a
  host is the customer saying that host is theirs.

What this does not reach: the agent's shell commands, stdio MCP servers
and ``command`` hooks are processes igni starts, and what they connect
to is the network's to stop. See the ``air_gapped`` comment in
``core/config/settings.py``.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

#: ``user@host:path`` or ``host:path`` — git's scp-like SSH syntax.
_SCP_LIKE = re.compile(r"^(?:[^@/\\]+@)?(?P<host>[^:/\\]+):(?!//)")
#: ``C:\\…`` or ``C:/…`` — a Windows drive, which the pattern above
#: would otherwise read as a host called ``C``.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
#: The one loopback name; addresses are recognised by ``ipaddress``.
_LOOPBACK_NAMES = frozenset({"localhost"})
#: How a pattern says "any host under this suffix".
_SUFFIX_PREFIX = "*."


class NetworkPolicy(BaseModel):
    """Decides whether a URL may be reached, given the deployment's settings."""

    air_gapped: bool = False
    #: Hosts inside the network: exact names or ``*.suffix`` patterns,
    #: lower-cased. Only consulted when ``air_gapped`` is set.
    internal_hosts: list[str] = Field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: object) -> NetworkPolicy:
        """Build from a :class:`~ember_code.core.config.settings.Settings`.

        Typed as ``object`` because session stubs in the test suite hand
        a partial settings object: a missing block contributes no hosts,
        and anything that is not literally ``True`` is not air-gapped.
        """
        network = getattr(settings, "network", None)
        named = getattr(network, "internal_hosts", None)
        plugins = getattr(settings, "plugins", None)
        marketplaces = getattr(plugins, "marketplaces", None)
        configured_urls = [
            getattr(settings, "api_url", ""),
            getattr(settings, "portal_url", ""),
            getattr(settings, "update_endpoint", ""),
            *(marketplaces if isinstance(marketplaces, list) else []),
        ]
        hosts = [h for h in named if isinstance(h, str)] if isinstance(named, list) else []
        hosts += [h for u in configured_urls if isinstance(u, str) and (h := cls.host_of(u))]
        return cls(
            air_gapped=getattr(settings, "air_gapped", False) is True,
            internal_hosts=hosts,
        )

    def allows(self, url: str) -> bool:
        """Whether reaching ``url`` keeps igni inside the customer's network."""
        if not self.air_gapped or self.is_local(url):
            return True
        host = self.host_of(url)
        return host is not None and self.is_inside(host)

    def is_inside(self, host: str) -> bool:
        """Whether ``host`` is loopback or one the customer named."""
        host = _canonical(host)
        if self.is_loopback(host):
            return True
        for pattern in self.internal_hosts:
            pattern = _canonical(pattern)
            if pattern.startswith(_SUFFIX_PREFIX):
                # ``*.acme.internal`` covers ``git.acme.internal`` and
                # ``a.b.acme.internal``, not ``acme.internal`` itself:
                # the apex is a host of its own and is named as one.
                if host.endswith(pattern[1:]):
                    return True
            elif host == pattern:
                return True
        return False

    def refusal(self, url: str, *, what: str = "it") -> str:
        """The message a refused URL is reported with, saying how to allow it."""
        host = self.host_of(url) or url
        return (
            f"{url} is outside this network and igni is air-gapped "
            f"(air_gapped: true), so {what} was not contacted. If {host} is "
            "inside your network, add it to network.internal_hosts in your "
            "user or managed settings."
        )

    @staticmethod
    def is_loopback(host: str) -> bool:
        """``localhost``, ``127.0.0.0/8`` or ``::1``."""
        host = _canonical(host)
        if host in _LOOPBACK_NAMES:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @staticmethod
    def is_local(url: str) -> bool:
        """A filesystem path or ``file://`` URL: read without a network."""
        stripped = url.strip()
        if stripped.lower().startswith("file://"):
            return True
        if "://" in stripped:
            return False
        if _WINDOWS_DRIVE.match(stripped):
            return True
        return _SCP_LIKE.match(stripped) is None

    @staticmethod
    def host_of(url: str) -> str | None:
        """The host a remote URL names, lower-cased; ``None`` for a local one."""
        stripped = url.strip()
        if not stripped or NetworkPolicy.is_local(stripped):
            return None
        if "://" in stripped:
            try:
                host = urlsplit(stripped).hostname
            except ValueError:
                return None
            return host.lower() if host else None
        match = _SCP_LIKE.match(stripped)
        return match.group("host").lower() if match else None


def _canonical(host: str) -> str:
    """Compare hosts without case, brackets or the root's trailing dot."""
    return host.strip().strip("[]").rstrip(".").lower()


__all__ = ["NetworkPolicy"]
