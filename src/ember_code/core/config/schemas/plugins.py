"""``plugins`` config — where plugin marketplaces come from.

Two independent switches, because they answer different questions:

* ``default_marketplace`` — whether igni registers Anthropic's official
  marketplace (``github.com/anthropics/claude-plugins-official``) at
  session start. Registering it clones that repository: an outbound
  call to github.com at every start whose catalog is stale. Off by
  default — a product sold to run without egress makes no call nobody
  asked for — and ignored under ``air_gapped: true``. ``/plugin
  marketplace add`` adds it, or any other, on request.
* ``marketplaces`` — the organisation's own marketplaces: a local path
  (a directory or a bare repository on a shared drive) or a git URL on
  a host inside the customer's network. Registered at session start
  the way the default one is.

Under ``air_gapped: true`` these are the *only* marketplaces the
session-start refresh touches: see
:class:`~ember_code.core.plugins.source_policy.PluginSourcePolicy`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PluginsConfig(BaseModel):
    #: Register Anthropic's official marketplace at session start. Opt-in;
    #: ignored (treated as off) when ``air_gapped`` is set.
    default_marketplace: bool = False
    #: The organisation's marketplaces: local paths or git URLs inside
    #: its network, registered at session start.
    marketplaces: list[str] = Field(default_factory=list)
