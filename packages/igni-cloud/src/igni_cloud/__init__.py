"""igni-cloud: igni's cloud layer, on top of the core (``ember_code``).

The core never imports this package. It finds it through two entry points
(``pyproject.toml``): the ``cloud`` layer (:class:`igni_cloud.layer.CloudLayer`),
installed when ``IGNI_PROFILE=cloud``, and PostgreSQL storage
(:class:`igni_cloud.storage.postgres.PostgresStorage`), used for a
``postgresql://`` database URL.
"""
