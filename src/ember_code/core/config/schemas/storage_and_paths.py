"""Path-oriented config schemas — ``storage``, ``auth``, ``code_index``.

Grouped into one file because each is a small path-oriented struct.
A per-schema file would bloat the schemas package with 10-line
modules without buying navigability.
"""

from __future__ import annotations

from pydantic import BaseModel

from ember_code.core.paths import DEFAULT_DATA_DIR


class StorageConfig(BaseModel):
    data_dir: str = DEFAULT_DATA_DIR
    audit_log: str = f"{DEFAULT_DATA_DIR}/audit.log"
    max_history_runs: int = 10000


class AuthConfig(BaseModel):
    credentials_file: str = f"{DEFAULT_DATA_DIR}/credentials.json"


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


class CodeIndexConfig(BaseModel):
    """Tunables for the local code-index sync.

    ``repository_id`` and the GCS bucket are auto-discovered from
    ``settings.api_url`` using the local git remote — users don't
    configure either.
    """

    fetch_timeout: float = 60.0
