"""Notifications: the floor telling people things (igni server's ``docs/service-identity.md`` §10).

* :mod:`.center` — :class:`~.center.NotificationCenter`, both paths merged.
* :mod:`.server_client` and :mod:`.stream` — igni server's routes and its stream.
* :mod:`.local_store` and :mod:`.extension_notices` — what an extension tells this machine.
* :mod:`.schemas` — the shapes, on their own so the protocol layer can
  import them without the rest.
"""
