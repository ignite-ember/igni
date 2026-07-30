"""Path-oriented config schemas — ``storage``, ``auth``, ``code_index``.

Grouped into one file because each is a small path-oriented struct.
A per-schema file would bloat the schemas package with 10-line
modules without buying navigability.
"""

from __future__ import annotations

from pydantic import BaseModel


class StorageConfig(BaseModel):
    data_dir: str = "~/.ember"
    audit_log: str = "~/.ember/audit.log"
    max_history_runs: int = 10000


class AuthConfig(BaseModel):
    credentials_file: str = "~/.ember/credentials.json"


class CodeIndexConfig(BaseModel):
    """Tunables for the local code-index sync.

    ``repository_id`` and the GCS bucket are auto-discovered from
    ``settings.api_url`` using the local git remote — users don't
    configure either.
    """

    fetch_timeout: float = 60.0
