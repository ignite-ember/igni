"""Media input coordinator — detect file paths and resolve references.

Two modes, both active. Which one runs depends on the model's
``vision`` flag in the registry (see ``backend/server.py:409``):

1. **Path resolution** — resolves bare filenames and relative paths to
   absolute paths so a text-only model can read them via Bash / cat /
   pdftotext. The path lands in the user message as a string.
2. **Media attachment** — for ``vision: true`` models only. Wraps the
   resolved paths in Agno ``Image`` / ``Audio`` / ``Video`` / ``File``
   objects which Agno serializes as base64-encoded content parts in
   the provider's chat-completions request body (OpenAI ``image_url``,
   Claude ``document``, etc.). The actual file bytes go on the wire
   on every turn that includes the attachment — see
   :meth:`MediaResolver.attach_local_paths` for the entry point.

A text-only model (e.g. MiniMax-M2.7) takes path 1 only; the bytes
stay on disk in ``.ember/attachments/<session_id>/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ember_code.core.utils.media_schemas import (
    MediaAttachments,
    MediaKind,
    MediaTaxonomy,
    ResolvedText,
)


class MediaResolver:
    """Turn free-form user text into resolved absolute paths and typed
    :class:`MediaAttachments` bundles.

    Constructor takes ``project_dir`` (the primary search location for
    bare filenames) plus an optional :class:`MediaTaxonomy` (tests
    can substitute a narrower one) and optional ``search_dirs``
    override (defaults preserve the pre-refactor behavior: project
    first, then Downloads / Desktop / Documents / home).

    The compiled regexes are built once per instance from the
    taxonomy's ``all_extensions`` — cheap enough that per-turn
    construction from PromptBuilder / runner / interactive_loop is
    fine. A future hoisting onto Session would be a signature-stable
    optimization if profiling ever asks for it.
    """

    # Default search directories for bare filenames. Instance-level
    # attribute so a caller can override without patching a module
    # constant.
    _DEFAULT_SEARCH_DIRS: tuple[Path, ...] = (
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home(),
    )

    def __init__(
        self,
        project_dir: Path | None = None,
        *,
        taxonomy: MediaTaxonomy | None = None,
        search_dirs: tuple[Path, ...] | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._taxonomy = taxonomy or MediaTaxonomy.default()
        self._search_dirs = search_dirs if search_dirs is not None else self._DEFAULT_SEARCH_DIRS

        # Build regex alternation from all known extensions
        # (without the dot). Compiled per instance so a custom
        # taxonomy in a test surfaces the right alt set.
        ext_alt = "|".join(ext.lstrip(".") for ext in sorted(self._taxonomy.all_extensions))
        self._url_pattern = re.compile(
            rf"(https?://\S+/\S+\.(?:{ext_alt})(?:\?\S*)?)", re.IGNORECASE
        )
        # Explicit paths (starts with ~, ., /, or drive letter)
        self._file_pattern = re.compile(
            rf"(?:^|\s)((?:[~/.]|\w:)[^\s]*\.(?:{ext_alt}))(?:\s|$)",
            re.IGNORECASE,
        )
        # Bare filenames without a path prefix ("photo.avif", "report.pdf")
        self._bare_file_pattern = re.compile(
            rf"(?:^|\s)([^\s/~.][^\s]*\.(?:{ext_alt}))(?:\s|$)",
            re.IGNORECASE,
        )

    # ── Path resolution ───────────────────────────────────────────────

    def _find_bare_file(self, filename: str) -> Path | None:
        """Search common directories for a bare filename.

        Checks the project directory first, then Downloads, Desktop,
        Documents, and home. Returns the first match or None.
        """
        candidates: list[Path] = []
        if self._project_dir:
            candidates.append(self._project_dir)
        candidates.extend(self._search_dirs)

        for directory in candidates:
            candidate = directory / filename
            if candidate.is_file():
                return candidate.resolve()
        return None

    def resolve_text_references(self, text: str) -> ResolvedText:
        """Resolve file references in user text to absolute paths.

        Replaces bare filenames and relative paths with their resolved
        absolute paths so the AI can use Read or other tools on them.

        Explicit paths are processed before bare filenames so a
        message like ``check ./photo.png and report.pdf`` resolves
        deterministically when both syntaxes appear.

        Returns a :class:`ResolvedText` — iterable so
        ``message, resolved = resolver.resolve_text_references(msg)``
        works as a tuple-unpack.
        """
        resolved: list[str] = []
        missed: list[str] = []

        # Explicit paths (~/..., ./..., /..., C:\...)
        for match in self._file_pattern.finditer(text):
            raw = match.group(1)
            path = Path(raw).expanduser().resolve()
            if path.is_file() and str(path) != raw:
                text = text.replace(raw, str(path))
                resolved.append(str(path))

        # Bare filenames — search common locations
        for match in self._bare_file_pattern.finditer(text):
            filename = match.group(1)
            found = self._find_bare_file(filename)
            if found:
                text = text.replace(filename, str(found))
                resolved.append(str(found))
            else:
                missed.append(filename)

        return ResolvedText(text=text, paths=resolved, missed=missed)

    # ── Media attachment (vision models) ──────────────────────────────

    def attach_local_paths(self, paths: list[str]) -> MediaAttachments | None:
        """Convert resolved file paths to a typed
        :class:`MediaAttachments` for vision models.

        Returns ``None`` if no attachable media was found so the
        caller can decide "text-only path" versus "attach and
        announce". The Agno container class is selected via
        :meth:`MediaTaxonomy.classify` → :meth:`MediaKindSpec.build`
        — no if/elif ladder.
        """
        buckets: dict[str, list[Any]] = {}

        for path_str in paths:
            p = Path(path_str)
            spec = self._taxonomy.classify(p.suffix)
            if spec is None:
                continue
            media_obj = spec.build(filepath=p)
            buckets.setdefault(spec.bucket_field, []).append(media_obj)

        if not buckets:
            return None
        return MediaAttachments.model_validate(buckets)

    def extract_url_media(self, text: str) -> MediaAttachments | None:
        """Extract media URLs from text and return as a typed
        :class:`MediaAttachments` for vision-capable models.

        Uses the same taxonomy dispatch as
        :meth:`attach_local_paths` — a new kind added to the
        taxonomy participates in both local and remote paths
        automatically.
        """
        buckets: dict[str, list[Any]] = {}

        for match in self._url_pattern.finditer(text):
            url = match.group(1)
            path_part = url.split("?")[0]
            ext = Path(path_part).suffix.lower()
            spec = self._taxonomy.classify(ext)
            if spec is None:
                continue
            media_obj = spec.build(url=url)
            buckets.setdefault(spec.bucket_field, []).append(media_obj)

        if not buckets:
            return None
        return MediaAttachments.model_validate(buckets)


# ── Backward-compatible free-function shims ──────────────────────────
#
# Existing tests (``tests/test_images.py``, ``tests/test_media_urls.py``)
# and any external importer still call the pre-refactor free
# functions. Each one below constructs a MediaResolver and delegates.
# New code should use MediaResolver directly.


def _classify_extension(ext: str) -> str:
    """Legacy dispatcher — returns the ``str`` value of the matching
    :class:`MediaKind` or ``"unknown"``. New code should use
    :meth:`MediaTaxonomy.classify` on a taxonomy instance."""
    spec = MediaTaxonomy.default().classify(ext)
    return spec.kind.value if spec is not None else MediaKind.unknown.value


def _find_bare_file(filename: str, project_dir: Path | None = None) -> Path | None:
    """Legacy helper. Prefer instantiating a
    :class:`MediaResolver` and calling its private ``_find_bare_file``
    — this thin shim exists only to preserve import-path
    compatibility for any straggling caller."""
    return MediaResolver(project_dir=project_dir)._find_bare_file(filename)


def resolve_file_references(text: str, project_dir: Path | None = None) -> tuple[str, list[str]]:
    """Legacy tuple-shaped path-resolution entry. Delegates to
    :meth:`MediaResolver.resolve_text_references`."""
    result = MediaResolver(project_dir=project_dir).resolve_text_references(text)
    return result.text, result.paths


def attach_resolved_files(paths: list[str]) -> dict[str, list[Any]] | None:
    """Legacy dict-shaped local-attachment entry. Delegates to
    :meth:`MediaResolver.attach_local_paths` and lowers the typed
    result back to the pre-refactor ``dict[str, list[Any]]`` shape
    so existing tests keep working."""
    media = MediaResolver().attach_local_paths(paths)
    if media is None:
        return None
    return media.to_kwargs()


def extract_media_urls(text: str) -> dict[str, list[Any]] | None:
    """Legacy dict-shaped URL-extraction entry. Delegates to
    :meth:`MediaResolver.extract_url_media` and lowers to the
    pre-refactor ``dict[str, list[Any]]`` shape."""
    media = MediaResolver().extract_url_media(text)
    if media is None:
        return None
    return media.to_kwargs()
