"""Typed wire schemas for the backend RPC surface + entry-point boot.

Extracted out of :mod:`ember_code.backend.__main__` where they used to
live inline alongside the boot code. Every wire shape the ``__main__``
composition root touches — either as an RPC response, an entry-point
boot payload, or a config value — is a Pydantic model here so mypy +
Ruff + Pydantic validation give schema coverage at the seam.

The original module re-exports every model in this file so external
callers (in-tree tests, other modules) don't need to update their
imports.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.core.auth.schemas import UserInfo
    from ember_code.core.utils.update_checker_schemas import UpdateInfo


# ── Ad-hoc RPC responses ──────────────────────────────────────────


class DirListResult(BaseModel):
    """Wire shape for the ``list_dirs`` RPC — GUI folder browser."""

    path: str
    parent: str
    dirs: list[str]
    home: str
    error: str


class PickDirResult(BaseModel):
    """Wire shape for the ``pick_dir_native`` RPC — OS folder picker."""

    path: str
    cancelled: bool
    error: str


class RunShellResult(BaseModel):
    """Wire shape for the ``run_shell`` RPC — ``$``-prefix shell mode."""

    output: str
    exit_code: int


class UpdateAvailable(BaseModel):
    """Wire shape for the ``check_for_update`` RPC — package updater
    notice. ``None`` means no update is available."""

    available: bool
    current_version: str
    latest_version: str
    download_url: str
    pkg_name: str

    @classmethod
    def from_update_info(cls, info: UpdateInfo, pkg_name: str) -> UpdateAvailable:
        """Pure wire-mapping from an :class:`UpdateInfo` domain
        object (network probe result) to the FE-facing payload.

        Stays pure data-shape — the network fetch + package metadata
        lookup live in the RPC handler so this module remains a leaf
        (no dependency on :mod:`core.utils.update_checker`).
        """
        return cls(
            available=True,
            current_version=info.current_version,
            latest_version=info.latest_version,
            download_url=info.download_url,
            pkg_name=pkg_name,
        )


class LoginStarted(BaseModel):
    """Wire shape for the ``login`` RPC — one-field ack that a login
    task is now in flight; the actual result arrives asynchronously
    via a ``login_result`` push notification."""

    started: bool


class LoginResult(BaseModel):
    """Named-field result for :meth:`AuthController.login`.

    Replaces the previous ``tuple[bool, str]`` where the second slot
    overloaded "email on success" / "error message on failure" —
    callers now read :attr:`ok`, :attr:`email`, :attr:`error`
    directly instead of pattern-matching on a positional pair.
    """

    ok: bool
    email: str | None = None
    error: str | None = None

    def wire_result_string(self) -> str:
        """Collapse to the single-string payload the
        ``on_login_result`` push notification expects: email on
        success, error text on failure, empty string when neither
        was recorded. Keeps the "success carries the email, failure
        carries the reason" wire convention on the model rather than
        at every call site."""
        if self.ok:
            return self.email or ""
        return self.error or ""


class CloudPlan(BaseModel):
    """Wire shape for :meth:`AuthController.get_cloud_plan` — tier +
    org name for the org popover badge. Nullable fields because the
    token validation response may omit either."""

    tier: str | None
    org_name: str | None

    @classmethod
    def from_user_info(cls, info: UserInfo) -> CloudPlan:
        """Map a :class:`UserInfo` (portal ``/me`` response) into the
        FE-facing plan payload. Wire-mapping lives on the model so
        :meth:`AuthController.get_cloud_plan` stays a two-liner."""
        return cls(tier=info.tier, org_name=info.org_display_name)


class FileCompletion(BaseModel):
    """Wire shape for the ``complete_files`` RPC — @-mention picker
    hits + a running total (used to render "N more matches" when the
    limit was reached)."""

    matches: list[str]
    total: int


class SkillDefinition(BaseModel):
    """Wire shape for one entry in the ``get_skill_definitions`` RPC
    response. Consumed by the FE autocomplete + SDK skill picker;
    ``prompt`` may be empty for manifest-only skills."""

    name: str
    description: str
    prompt: str


class DisplayConfigResult(BaseModel):
    """Wire shape for the ``get_display_config`` RPC — a flexible
    view of ``settings.display`` used by the FE to render badges /
    theming / toggles. Additional fields are allowed because the
    Settings.display model may evolve independently of this schema."""

    model_config = {"extra": "allow"}

    @classmethod
    def from_display(cls, display: Any) -> DisplayConfigResult:
        """Normalise the ``settings.display`` value into the wire
        shape. Handles the Pydantic-model-or-raw-dict duality so the
        RPC handler stays a one-liner."""
        dumped = display.model_dump() if hasattr(display, "model_dump") else {}
        return cls.model_validate(dumped)


class ModelRegistryResult(BaseModel):
    """Wire shape for the ``get_model_registry`` RPC — the FE's
    model-picker source of truth (default, ceiling, per-model
    metadata rows)."""

    default: str
    max_context_window: int
    registry: dict

    @classmethod
    def from_settings(cls, models: Any) -> ModelRegistryResult:
        """Normalise a ``settings.models`` value into the wire shape.

        Registry values are heterogeneous — cloud discovery writes
        typed :class:`ModelRegistryEntry` instances alongside raw
        dicts loaded from user YAML. Collapse everything to dicts for
        the wire (which is a plain ``dict``).
        """
        wire_registry = {
            name: (entry.model_dump() if hasattr(entry, "model_dump") else entry)
            for name, entry in models.registry.items()
        }
        return cls(
            default=models.default,
            max_context_window=models.max_context_window,
            registry=wire_registry,
        )


# ── Pool-level RPC responses ───────────────────────────────────────


class AttachSessionResult(BaseModel):
    """Wire shape for the ``attach_session`` RPC — the FE learns
    which session id + project directory the pool actually bound."""

    session_id: str
    project_dir: str


class GetClientStateResult(BaseModel):
    """Wire shape for the ``get_client_state`` RPC — every stored
    key/value the client's opaque ``client_id`` has ever written."""

    state: dict[str, str]


class WriteClientStateResult(BaseModel):
    """Wire shape for ``set_client_state`` / ``delete_client_state``
    — write-side ack."""

    ok: bool


# ── Boot-time payloads ────────────────────────────────────────────


class BackendReadyLine(BaseModel):
    """Line one of BE stdout: the "I'm ready, connect here" payload
    the parent process parses to learn transport specifics. Golden
    string: dumped via ``model_dump_json(exclude_none=True)`` so the
    FE's byte-oriented parser sees the exact same bytes it always
    has (the boot line is stable wire, not a versioned RPC)."""

    status: str = "ready"
    ws_port: int | None = None
    ws_url: str | None = None
    socket: str | None = None


class SessionPoolConfig(BaseModel):
    """Typed config for :class:`SessionPool` construction. The pool
    itself still accepts ``idle_timeout_seconds`` as a kwarg — we
    thread this via ``**config.model_dump(exclude_none=True)`` at
    the call site rather than flipping the pool's constructor, so
    other callers keep working unchanged.

    The pool's constructor takes typed seams
    (:class:`ember_code.backend.schemas_sessions.BackendLike` /
    :class:`ember_code.backend.schemas_sessions.TransportLike`
    Protocols) on its runtimes' wiring — so downstream mypy sees
    the exact backend/transport surface the pool relies on."""

    idle_timeout_seconds: float | None = None


# ── Lifecycle ─────────────────────────────────────────────────────


class LifecyclePhase(str, Enum):
    """Single-source-of-truth shutdown state for
    :class:`BackendSupervisor`. Replaces the four independent boolean
    flags the old ``_run`` juggled (parent_watch_task cancelled?
    shutdown_close_task cancelled? evictor_task cancelled? pool
    shutdown?)."""

    BOOTING = "booting"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
