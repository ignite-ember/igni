"""Assembles the pre-run user prompt.

:class:`PromptBuilder` turns a raw user message into the composed
prompt handed to ``team.arun``: resolves ``@file`` mentions,
attaches media, fires learnings injection, and wraps the text in
a ``<system-context>`` header. Returns a typed
:class:`PromptBuildResult` carrying the message, resolved media,
and an ordered list of user-visible info messages.

Composed with a :class:`Session` (for ``project_dir`` + models
registry + learnings). The RunController threads
interrupted-summary state through :meth:`build` explicitly rather
than the builder reaching for private session state.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.schemas_run import (
    MediaAttachments,
    ModelConfig,
    PromptBuildResult,
)
from ember_code.core.config.model_entry import ModelRegistryEntry
from ember_code.core.utils.media import MediaResolver
from ember_code.core.utils.mentions import process_file_mentions
from ember_code.protocol import messages as msg


class _FileAttachStep(BaseModel):
    """Pipeline-step result carrying the text/media pair plus any
    user-visible info messages the caller should surface."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    media: MediaAttachments | None = None
    info_messages: list[msg.Info] = Field(default_factory=list)


class _UrlAttachStep(BaseModel):
    """Pipeline-step result for URL-media extraction."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    media: MediaAttachments | None = None
    info_messages: list[msg.Info] = Field(default_factory=list)


if TYPE_CHECKING:
    from ember_code.core.session import Session


class PromptBuilder:
    """Turn a raw user prompt into the fully-composed message we
    pass to ``team.arun``.

    Pipeline:

    1. ``@file`` mentions → substitute + announce.
    2. Bare filename references → resolve; on vision-capable models
       attach as media.
    3. URL media (images in the text) → attach on vision models.
    4. Inject learnings (fire-and-forget onto the team's
       instructions list).
    5. Compose the ``<system-context>`` header with timestamp and
       optional interrupted-run recap.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._resolver = MediaResolver(project_dir=session.project_dir)

    async def build(
        self,
        text: str,
        media: MediaAttachments | None,
        *,
        interrupted_summary: str | None,
    ) -> PromptBuildResult:
        """Run the full pipeline. Returns typed result — caller
        yields ``info_messages`` in order, then uses ``message`` +
        ``media`` as the Agno inputs."""
        info: list[msg.Info] = []
        text, mentioned_files = process_file_mentions(text)
        if mentioned_files:
            info.append(msg.Info(text=f"Referenced: {', '.join(mentioned_files)}"))

        model_cfg = self._resolve_model_config()
        file_step = self._attach_file_references(text, media, model_cfg)
        text, media = file_step.text, file_step.media
        info.extend(file_step.info_messages)
        url_step = self._attach_url_media(text, media, model_cfg)
        media = url_step.media
        info.extend(url_step.info_messages)

        # Injects into the team's instructions. Fire-and-forget onto
        # ``main_team.instructions`` — no data flows back through
        # the return value.
        await self._session.inject_learnings()

        if interrupted_summary:
            info.append(msg.Info(text="(continuing from an interrupted previous run)"))
        message = self._compose_message(text, interrupted_summary)

        return PromptBuildResult(message=message, media=media, info_messages=info)

    def _resolve_model_config(self) -> ModelConfig:
        """Validate the current default model's row from the settings
        registry, returning an all-defaults ``ModelConfig`` when the
        entry is missing or not schema-shaped.

        Registry values are heterogeneous — cloud discovery writes
        typed :class:`ModelRegistryEntry` instances while user YAML
        loads as raw dicts. Both flow through Pydantic validation
        via ``model_dump`` or a direct dict passthrough."""
        registry = self._session.settings.models.registry
        name = self._session.settings.models.default
        entry = registry.get(name)
        if isinstance(entry, ModelRegistryEntry):
            return ModelConfig.model_validate(entry.model_dump())
        if isinstance(entry, dict):
            return ModelConfig.model_validate(entry)
        return ModelConfig()

    def _attach_file_references(
        self,
        text: str,
        media: MediaAttachments | None,
        model_cfg: ModelConfig,
    ) -> _FileAttachStep:
        """Resolve bare filenames + attach as media on vision
        models. Non-vision models get a text-only "Resolved" line.
        Returns a :class:`_FileAttachStep` bundle — the caller
        merges ``info_messages`` into its running list.
        """
        resolved = self._resolver.resolve_text_references(text)
        text = resolved.text
        resolved_files = resolved.paths
        if not resolved_files:
            return _FileAttachStep(text=text, media=media)
        if model_cfg.vision:
            parsed_media = self._resolver.attach_local_paths(resolved_files)
            if parsed_media is not None:
                return _FileAttachStep(
                    text=text,
                    media=parsed_media,
                    info_messages=[msg.Info(text=f"Attached: {len(resolved_files)} file(s)")],
                )
        return _FileAttachStep(
            text=text,
            media=media,
            info_messages=[msg.Info(text=f"Resolved: {', '.join(resolved_files)}")],
        )

    def _attach_url_media(
        self,
        text: str,
        media: MediaAttachments | None,
        model_cfg: ModelConfig,
    ) -> _UrlAttachStep:
        """Extract media URLs from the text and merge into the
        attachments bundle on vision models. Returns a
        :class:`_UrlAttachStep` bundle — the caller merges
        ``info_messages`` into its running list.
        """
        if not model_cfg.vision:
            return _UrlAttachStep(media=media)
        url_media = self._resolver.extract_url_media(text)
        if url_media is None:
            return _UrlAttachStep(media=media)
        if media is None:
            media = url_media
        else:
            media.merge(url_media)
        return _UrlAttachStep(
            media=media,
            info_messages=[msg.Info(text=f"Attached {url_media.total()} URL(s)")],
        )

    @staticmethod
    def _compose_message(text: str, interrupted_summary: str | None) -> str:
        """Wrap the user text with a ``<system-context>`` header
        carrying the current timestamp and (if present) the
        one-shot interrupted-run recap."""
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        parts = [f"Current datetime: {timestamp}"]
        if interrupted_summary:
            parts.append(interrupted_summary)
        return f"<system-context>{' '.join(parts)}</system-context>\n{text}"
