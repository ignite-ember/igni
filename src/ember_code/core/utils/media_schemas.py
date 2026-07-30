"""Typed schemas + taxonomy for media detection & attachment.

Extracted from :mod:`ember_code.core.utils.media` — the free-function
pile that used a raw ``str`` return type (``_classify_extension``),
two duplicated ``if/elif`` ladders (``attach_resolved_files`` and
``extract_media_urls``), and returned ``dict[str, list[Any]]`` for
Agno's media kwargs. Every kind (image/audio/video/document) now
lives as one :class:`MediaKindSpec` row inside :class:`MediaTaxonomy`;
polymorphic construction goes through :meth:`MediaKindSpec.build`.

Contents:

* :class:`MediaKind` — string enum replacing the untyped ``str``
  return of the old ``_classify_extension`` free function.
* :class:`MediaKindSpec` — frozen dataclass, one row per kind.
  Carries the extension set, the Agno container class, the
  :class:`MediaAttachments` bucket field name, and a
  :meth:`build` method that constructs the Agno object from either
  a local filepath or a remote URL. Kills both if/elif ladders.
* :class:`MediaTaxonomy` — small object holding a tuple of specs
  with :meth:`classify` for ext lookup and :meth:`default`
  classmethod for the current 4-kind taxonomy.
* :class:`MediaAttachments` — Pydantic model, moved down from
  :mod:`ember_code.backend.schemas_run` so
  :class:`~ember_code.core.utils.media.MediaResolver` can produce
  it directly. Adds :meth:`merge` (typed) and :meth:`total`
  (int summary) replacing the dict-shaped ``merge_urls`` /
  ``sum(len(v) for v in .values())`` idioms.
* :class:`ResolvedText` — Pydantic model, return type of
  :meth:`MediaResolver.resolve_text_references`. Iterable so
  ``message, resolved = resolver.resolve_text_references(msg)``
  keeps the tuple-unpacking shape from the pre-refactor free
  function.

``MediaAttachments`` is re-exported from
:mod:`ember_code.backend.schemas_run` for import-path compatibility
with all pre-refactor callers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agno.media import Audio, File, Image, Video
from pydantic import BaseModel, ConfigDict, Field


class MediaKind(str, Enum):
    """Typed replacement for the ``str`` return of the old
    ``_classify_extension`` — the four media kinds Agno's
    per-provider plugins accept as attachment containers.

    :attr:`unknown` is the "no match" sentinel for callers that
    still branch on the classification result (e.g. tests).
    """

    image = "image"
    audio = "audio"
    video = "video"
    document = "document"
    unknown = "unknown"


@dataclass(frozen=True)
class MediaKindSpec:
    """One row of the media taxonomy — the extension → container
    mapping used to be a string return + two if/elif ladders. Now
    it's one row with :meth:`build` handling both local and remote
    attachment construction.

    ``bucket_field`` is the attribute name on :class:`MediaAttachments`
    (``images`` / ``audio`` / ``videos`` / ``files``). It differs from
    ``kind.value`` for images (bucket ``images``) and videos
    (bucket ``videos``) — hence the explicit field.
    """

    kind: MediaKind
    extensions: frozenset[str]
    container_cls: type[Image | Audio | Video | File]
    bucket_field: str

    def build(
        self,
        *,
        url: str | None = None,
        filepath: Path | None = None,
    ) -> Image | Audio | Video | File:
        """Construct the Agno media container.

        Exactly one of ``url`` / ``filepath`` must be provided —
        Agno's constructors accept either, and a silent both-set
        would let the constructor pick one non-deterministically.
        """
        assert (url is None) ^ (filepath is None), (
            "MediaKindSpec.build requires exactly one of url / filepath"
        )
        if url is not None:
            return self.container_cls(url=url)
        return self.container_cls(filepath=filepath)


@dataclass(frozen=True)
class MediaTaxonomy:
    """Ordered tuple of :class:`MediaKindSpec` rows + an ext lookup.

    Adding a new kind (``.epub`` → document, or a new
    ``spreadsheet`` bucket) is one row in :meth:`default`. The
    old code required edits across three extension constants and
    two if/elif ladders.
    """

    specs: tuple[MediaKindSpec, ...]

    def classify(self, ext: str) -> MediaKindSpec | None:
        """Return the spec whose extension set contains ``ext``,
        or ``None`` if no spec matches. Case-insensitive."""
        needle = ext.lower()
        for spec in self.specs:
            if needle in spec.extensions:
                return spec
        return None

    @property
    def all_extensions(self) -> frozenset[str]:
        """All known extensions across every kind — used to build
        the URL / file regex alternation in
        :class:`~ember_code.core.utils.media.MediaResolver`."""
        return frozenset().union(*(s.extensions for s in self.specs))

    @classmethod
    def default(cls) -> MediaTaxonomy:
        """The current 4-kind taxonomy. Pinned as a classmethod so
        the default set is one call away, but callers can pass a
        custom taxonomy to :class:`MediaResolver` for tests."""
        return cls(
            specs=(
                MediaKindSpec(
                    kind=MediaKind.image,
                    extensions=frozenset(
                        {
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".gif",
                            ".webp",
                            ".bmp",
                            ".tiff",
                            ".tif",
                            ".svg",
                            ".avif",
                            ".heic",
                            ".heif",
                        }
                    ),
                    container_cls=Image,
                    bucket_field="images",
                ),
                MediaKindSpec(
                    kind=MediaKind.audio,
                    extensions=frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma"}),
                    container_cls=Audio,
                    bucket_field="audio",
                ),
                MediaKindSpec(
                    kind=MediaKind.video,
                    extensions=frozenset({".mp4", ".mov", ".avi", ".webm", ".mkv", ".wmv"}),
                    container_cls=Video,
                    bucket_field="videos",
                ),
                MediaKindSpec(
                    kind=MediaKind.document,
                    extensions=frozenset({".pdf"}),
                    container_cls=File,
                    bucket_field="files",
                ),
            )
        )


class MediaAttachments(BaseModel):
    """Typed replacement for the ``dict[str, Any]`` ``media`` kwargs
    threaded through ``run_message`` → ``team.arun``.

    Agno's per-provider model plugins accept different media kinds
    (images / audio / video / files); the exact set is
    provider-dependent and grows as new plugins land. We name the
    common ones explicitly for static coverage and keep
    ``extra='allow'`` so a provider-specific key still round-trips
    at the protocol boundary. The one place we lower back to a
    dict is :meth:`to_kwargs` — the Agno interop seam.

    Moved from :mod:`ember_code.backend.schemas_run` so
    :class:`~ember_code.core.utils.media.MediaResolver` can produce
    the typed model directly (previously the resolver returned a
    raw dict and PromptBuilder ran ``model_validate`` at the seam).
    Re-exported from the old location for wire-compat.
    """

    model_config = ConfigDict(extra="allow")

    images: list[Any] = Field(default_factory=list)
    audio: list[Any] = Field(default_factory=list)
    videos: list[Any] = Field(default_factory=list)
    files: list[Any] = Field(default_factory=list)

    def to_kwargs(self) -> dict[str, Any]:
        """Lower to ``**kwargs`` for ``team.arun(**media.to_kwargs())``.

        Uses raw attribute access — the buckets hold live Agno
        ``Image``/``Audio``/``Video``/``File`` instances that must
        reach ``team.arun`` unchanged. ``model_dump`` would recurse
        into them (they're Pydantic too) and turn them into dicts
        that Agno rejects.

        Omits empty collections so the Agno call site is the same
        shape as the pre-refactor raw-dict path (empty ``images=[]``
        would cause some providers to send an empty images-array
        rather than skip the field)."""
        out: dict[str, Any] = {}
        # Declared buckets first — always present as attributes.
        for name in ("images", "audio", "videos", "files"):
            v = getattr(self, name)
            if v:
                out[name] = v
        # ``extra='allow'`` extension buckets — future-media forward
        # compat. ``model_extra`` returns the raw stored objects.
        for name, v in (self.__pydantic_extra__ or {}).items():
            if v not in (None, [], {}):
                out[name] = v
        return out

    def is_empty(self) -> bool:
        """True when no attachment payload is set — the caller can
        skip the ``**media.to_kwargs()`` splat entirely."""
        return not self.to_kwargs()

    def total(self) -> int:
        """Total attachment count across every declared bucket.

        Replaces the ``sum(len(v) for v in raw_dict.values())``
        idiom PromptBuilder used to run after ``extract_media_urls``
        returned a dict."""
        return sum(len(v) for v in self.to_kwargs().values() if isinstance(v, list))

    @classmethod
    def from_optional_dict(cls, raw: dict[str, Any] | None) -> MediaAttachments | None:
        """Build from the legacy ``dict[str, Any] | None`` shape kept
        on the ``run_message`` signature for FE compatibility.

        Empty dicts collapse to ``None`` so callers can treat "no
        media" uniformly."""
        if not raw:
            return None
        return cls.model_validate(raw)

    def merge(self, other: MediaAttachments) -> None:
        """Merge another attachments bundle into this one in place.

        Preserves existing entries and extends per-kind lists.
        Supersedes the dict-shaped ``merge_urls`` — the resolver
        now returns a typed :class:`MediaAttachments` directly."""
        for k, v in other.to_kwargs().items():
            if not isinstance(v, list):
                continue
            existing = getattr(self, k, None)
            if isinstance(existing, list):
                existing.extend(v)
            else:
                # ``extra='allow'`` path — attribute may not be a
                # declared field. Fall back to attr set.
                setattr(self, k, list(v))

    def merge_urls(self, url_media: dict[str, list[Any]]) -> None:
        """Backward-compatible shim for the pre-refactor dict-shaped
        merge. New code should use :meth:`merge` with a typed
        :class:`MediaAttachments`. Kept because the wire-compat
        re-export from ``schemas_run`` promises the same public
        surface as before."""
        for k, v in url_media.items():
            existing = getattr(self, k, None)
            if isinstance(existing, list):
                existing.extend(v)
            else:
                setattr(self, k, list(v))


class ResolvedText(BaseModel):
    """Return type of :meth:`MediaResolver.resolve_text_references`.

    Replaces the old ``tuple[str, list[str]]`` return of the free
    function. Iterable so callers that unpack the tuple form —
    ``message, resolved = resolver.resolve_text_references(msg)`` —
    keep working without a signature-migration diff.

    :attr:`missed` surfaces bare-filename lookups that failed so a
    future :class:`PromptBuilder` Info message can warn the user
    ("photo.png not found in project / Downloads / Desktop / …")
    instead of silently dropping the reference. Not yet consumed
    by any caller — reserved for the follow-up UX pass.
    """

    text: str
    paths: list[str] = Field(default_factory=list)
    missed: list[str] = Field(default_factory=list)

    def __iter__(self) -> Iterator[Any]:  # type: ignore[override]
        """Yield ``(text, paths)`` so the old tuple-unpacking
        callsites stay a one-line migration."""
        yield self.text
        yield self.paths
