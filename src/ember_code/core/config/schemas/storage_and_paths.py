"""Path-oriented config schemas — ``storage``, ``auth``, ``code_index``.

Grouped into one file because each is a small path-oriented struct.
A per-schema file would bloat the schemas package with 10-line
modules without buying navigability.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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
    data_dir: str = Field(default_factory=_home_config_path)
    audit_log: str = Field(default_factory=lambda: _home_config_path("audit.log"))
    max_history_runs: int = 10000


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


class CodeIndexConfig(BaseModel):
    """Tunables for the local code-index sync.

    ``repository_id`` and the GCS bucket are auto-discovered from
    ``settings.api_url`` using the local git remote — users don't
    configure either.
    """

    #: Whether this session has a code index at all.
    #:
    #: Off means the ``CodeIndex`` tool is never offered, the plain
    #: ``main_agent`` prompt is used instead of the CodeIndex-first
    #: variant, and the Neo4j sidecar is not attached — so no
    #: distribution download, no server process, no refcount. That last
    #: part is the point: the index is backed by Neo4j, and somebody who
    #: is not reading code should not pay for a graph database.
    #:
    #: Meant to be server-settable: a group's ``settings`` entry is designed
    #: to deep-merge into config above both CLI flags and project files, so an
    #: admin could turn this off for a group and nobody could turn it back on
    #: locally::
    #:
    #:     {"code_index": {"enabled": false}}
    #:
    #: **That path does not run today.** ``GroupPolicyTier`` is never
    #: constructed in production and the pack's ``settings`` entries are never
    #: written to the on-disk cache, so an admin publishing the block above
    #: changes nothing on the client and nothing says so. See the "Nothing
    #: constructs this in production yet" note on
    #: :class:`~ember_code.core.config.merge_plan.GroupPolicyTier`.
    #:
    #: **This alone does not remove Neo4j.** ``artifacts.enabled`` is
    #: backed by the same sidecar, and the sidecar is only skipped when
    #: neither feature wants it. A deployment that wants no graph
    #: database at all turns off both.
    enabled: bool = True

    fetch_timeout: float = 60.0
