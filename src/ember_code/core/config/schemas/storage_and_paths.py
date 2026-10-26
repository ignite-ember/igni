"""Path-oriented config schemas — ``storage`` and ``auth``.

Grouped into one file because each is a small path-oriented struct.
A per-schema file would bloat the schemas package with 10-line
modules without buying navigability.
"""

from __future__ import annotations

import os
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from ember_code.core.paths import home_config_dir


def _home_config_path(*parts: str) -> str:
    """The home config directory *this machine actually uses*, plus ``parts``.

    These three defaults used to be built from ``DEFAULT_DATA_DIR``, which is
    unconditionally the post-rename ``~/.igni``. The settings loader, by
    contrast, reads config through ``home_config_dir()``, which falls back to
    the pre-rename ``~/.ember`` while that is the directory on disk. So an
    install that has not been migrated read its config from one directory and
    looked for ``credentials.json`` and ``client_state.db`` in another that
    does not exist — it came up signed out with no history, which is exactly
    the outcome ``dir_migration``'s docstring says must not happen ("a rename
    that half happens signs somebody out").

    Resolved through a ``default_factory`` rather than a module constant
    because the answer depends on what is on the filesystem, and a constant
    evaluated at import time would freeze whichever answer was true then —
    the reason ``paths.DEFAULT_DATA_DIR`` deliberately does *not* consult the
    filesystem and so cannot be used here.
    """
    return str(home_config_dir().joinpath(*parts))


class StorageConfig(BaseModel):
    #: The variable a deployment names its database with.
    DATABASE_URL_ENV: ClassVar[str] = "IGNI_DATABASE_URL"

    data_dir: str = Field(default_factory=_home_config_path)
    audit_log: str = Field(default_factory=lambda: _home_config_path("audit.log"))
    max_history_runs: int = 10000
    #: Where igni's state lives when it is not the desktop's files: a
    #: SQLAlchemy URL, PostgreSQL in the cloud (``docs/CLOUD.md`` §9).
    #: Unset, each project keeps its ``state.db`` under ``data_dir`` as
    #: it always has. ``IGNI_DATABASE_URL`` wins over the config file —
    #: it is a deployment's decision, made where the process is started.
    database_url: str | None = None

    @model_validator(mode="after")
    def _database_url_from_environment(self) -> StorageConfig:
        configured = os.environ.get(self.DATABASE_URL_ENV, "").strip()
        if configured:
            self.database_url = configured
        return self


class AuthConfig(BaseModel):
    credentials_file: str = Field(default_factory=lambda: _home_config_path("credentials.json"))


class GroupPolicyConfig(BaseModel):
    """How often to ask the server what this person's group holds.

    Until now the answer arrived once, at backend start: an admin who
    moved somebody between groups at nine o'clock reached a session
    opened at eight only when it was restarted.

    The poll is cheap — the server answers 304 with no body when
    nothing has changed — so the interval trades latency against very
    little. Five minutes matches the cache TTL, so there is one number
    rather than two. Zero turns polling off, for a deployment that would
    rather its machines only check at startup.
    """

    poll_seconds: int = 300
