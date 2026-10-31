"""Which plugin marketplaces igni may reach, given the deployment's settings.

Every marketplace operation is a ``git clone`` of the marketplace's
repository. On an ordinary install any URL is fine. Under
``air_gapped: true`` only the customer's own sources are:

* a **local** source — a filesystem path or a ``file://`` URL (a
  directory, or a bare repository on a shared drive), which git reads
  without touching the network;
* a URL listed in ``plugins.marketplaces``, or any URL on the same
  host as one: naming a marketplace on a host is the customer saying
  that host is inside their network.

The URL and host rules are :class:`~ember_code.core.network_policy.NetworkPolicy`'s,
the one policy every connection igni starts goes through; this class
supplies the hosts a marketplace may come from and the message a
refused one is reported with.

Everything else — Anthropic's official marketplace on github.com, or a
public marketplace somebody registered before the switch was turned
on — is refused before git runs, so no connection is attempted.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ember_code.core.network_policy import NetworkPolicy
from ember_code.core.plugins.marketplace_store import DEFAULT_MARKETPLACES


class PluginSourcePolicy(BaseModel):
    """Decides whether a marketplace URL may be fetched."""

    air_gapped: bool = False
    default_marketplace: bool = True
    #: The customer's own marketplaces (``plugins.marketplaces``).
    customer_marketplaces: list[str] = Field(default_factory=list)
    #: Hosts the customer marked as inside (``network.internal_hosts``),
    #: so a host listed there works for plugins as it does for
    #: ``igni ext install``, MCP servers and HTTP hooks.
    internal_hosts: list[str] = Field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: object) -> PluginSourcePolicy:
        """Build from a :class:`~ember_code.core.config.settings.Settings`.

        Typed as ``object`` because session stubs in the test suite
        hand a partial settings object; a missing block means the
        built-in default, and anything that is not literally ``True``
        is not air-gapped.
        """
        plugins = getattr(settings, "plugins", None)
        marketplaces = getattr(plugins, "marketplaces", None)
        return cls(
            air_gapped=getattr(settings, "air_gapped", False) is True,
            default_marketplace=getattr(plugins, "default_marketplace", True) is not False,
            customer_marketplaces=list(marketplaces) if isinstance(marketplaces, list) else [],
            internal_hosts=NetworkPolicy.from_settings(settings).internal_hosts,
        )

    def marketplaces_to_register(self) -> list[str]:
        """URLs session start should register: the default one when allowed,
        then the customer's own."""
        defaults = (
            [url for _name, url in DEFAULT_MARKETPLACES]
            if self.default_marketplace and not self.air_gapped
            else []
        )
        return [*defaults, *self.customer_marketplaces]

    def allows(self, url: str) -> bool:
        """Whether fetching ``url`` keeps igni inside the customer's network."""
        if _normalise(url) in {_normalise(u) for u in self.customer_marketplaces}:
            return True
        return self._network().allows(url)

    def refusal(self, url: str) -> str:
        """The message a refused URL is reported with."""
        return (
            f"{url} is outside this network and igni is air-gapped "
            "(air_gapped: true). Use a local path, list the marketplace "
            "under plugins.marketplaces, or add its host to network.internal_hosts."
        )

    def _network(self) -> NetworkPolicy:
        """The shared policy, with the customer's marketplace hosts as its
        inside. Built per call: the fields are public and may be changed."""
        hosts = [self.host_of(u) for u in self.customer_marketplaces]
        return NetworkPolicy(
            air_gapped=self.air_gapped,
            internal_hosts=[*self.internal_hosts, *(h for h in hosts if h)],
        )

    @staticmethod
    def is_local(url: str) -> bool:
        """A filesystem path or ``file://`` URL: git reads it without a network."""
        return NetworkPolicy.is_local(url)

    @staticmethod
    def host_of(url: str) -> str | None:
        """The host a remote URL names, lower-cased; ``None`` for a local one."""
        return NetworkPolicy.host_of(url)


def _normalise(url: str) -> str:
    """Compare URLs without the trailing ``/`` or ``.git`` people vary on."""
    stripped = url.strip().rstrip("/")
    return stripped[:-4] if stripped.endswith(".git") else stripped


__all__ = ["PluginSourcePolicy"]
