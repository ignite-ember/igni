"""Application settings as a process-wide singleton.

Pattern works fine for read-mostly config but couples every consumer
to a global. Tests have to monkey-patch the module to override values.
Tagged ``patterns=[singleton]``, ``testability=difficult``,
``concerns=[testability]``, ``quality=fair``.
"""

from __future__ import annotations

import os


class _Settings:
    """Holds runtime config. Built once at import time."""

    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./local.db")
        self.cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", "300"))
        self.max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "10"))
        self.debug = os.getenv("DEBUG", "false").lower() == "true"


# The singleton — every importer gets this same instance.
SETTINGS = _Settings()


def reload() -> None:
    """Re-read environment variables into the singleton.

    Tests use this when they need to override env after import. The
    fact that this exists is the smell — direct DI would obviate it.
    """
    global SETTINGS
    SETTINGS = _Settings()
