"""Render an artifact and its lineage as one self-contained HTML page.

Two properties make this file what it is:

**Self-contained.** No external stylesheet, no script tag pointing at
a CDN, no remote font. The output is one file that opens from disk,
survives being emailed, and prints to PDF without a network. That is
also what makes it safe to hand to the share path — a page that
fetches anything at view time is a page that phones home from
someone else's machine.

**Projected, not dumped.** :class:`ExportScope` decides what leaves.
Chunks, embeddings, session ids and candidate edges never appear in
output; a derivation edge pointing at an artifact outside the shared
set is dropped entirely rather than rendered as a dangling reference,
because a dangling reference leaks the name and the existence of
private work.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from ember_code.core.artifacts.models import (
    ArtifactLineage,
    ArtifactSummary,
    ArtifactVersion,
    EdgeState,
    LineageEdge,
)


@dataclass
class ExportScope:
    """Which artifacts are inside the boundary being rendered.

    ``None`` means "local view" — everything is in scope, because the
    page is not leaving the machine. A populated set means an export:
    edges to anything outside it are removed.
    """

    artifact_ids: set[str] | None = None
    include_candidates: bool = True
    include_dependents: bool = True

    @classmethod
    def local(cls) -> ExportScope:
        return cls(artifact_ids=None)

    @classmethod
    def shared(cls, artifact_ids: set[str]) -> ExportScope:
        # Candidates are unreviewed guesses about provenance. Shipping
        # them outward asserts a relationship nobody confirmed.
        return cls(
            artifact_ids=set(artifact_ids),
            include_candidates=False,
            include_dependents=True,
        )

    def contains(self, artifact_id: str) -> bool:
        return self.artifact_ids is None or artifact_id in self.artifact_ids


@dataclass
class RenderRequest:
    """Everything the page needs, already fetched."""

    summary: ArtifactSummary
    version: ArtifactVersion
    lineage: ArtifactLineage | None = None
    history: list[ArtifactVersion] = field(default_factory=list)
    scope: ExportScope = field(default_factory=ExportScope.local)
    title_suffix: str = ""


class ArtifactRenderer:
    """Turns an artifact + its lineage into a standalone HTML document."""

    def render(self, request: RenderRequest) -> str:
        summary = request.summary
        version = request.version
        title = summary.name or summary.artifact_id
        parts = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_esc(title)}{_esc(request.title_suffix)}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            '<main class="page">',
            self._header(request),
            self._body(version),
            self._history(request),
            self._lineage(request),
            "</main>",
            "</body>",
            "</html>",
        ]
        return "\n".join(p for p in parts if p)

    # ── Sections ──────────────────────────────────────────────────────

    def _header(self, request: RenderRequest) -> str:
        summary = request.summary
        version = request.version
        stale = self._stale_edges(request)
        banner = ""
        if stale:
            items = "".join(
                f"<li>{_esc(e.to_name or e.to_artifact_id)} moved from "
                f"v{e.pinned_n} to v{e.source_head_n}</li>"
                for e in stale
            )
            banner = (
                '<div class="banner" role="status">'
                "<strong>Sources have changed since this version was written.</strong>"
                f"<ul>{items}</ul></div>"
            )
        meta = [
            f"v{version.n}",
            "current" if version.head else "superseded",
            _esc(summary.kind or "note"),
        ]
        if version.created_at:
            meta.append(_esc(_date(version.created_at)))
        if version.author:
            meta.append(_esc(version.author))
        if summary.is_imported:
            meta.append("imported")
        return (
            "<header>"
            f"<h1>{_esc(summary.name or summary.artifact_id)}</h1>"
            f'<p class="meta">{" · ".join(meta)}</p>'
            f'<p class="id">{_esc(summary.artifact_id)}</p>'
            f"{banner}"
            "</header>"
        )

    def _body(self, version: ArtifactVersion) -> str:
        return f'<article class="content">{_markdownish(version.content)}</article>'

    def _history(self, request: RenderRequest) -> str:
        """The version switcher.

        Rendered as plain anchors to sibling files rather than a
        script-driven picker: the page has to work from disk and
        inside a PDF, where nothing runs.
        """
        if len(request.history) < 2:
            return ""
        rows = []
        for v in request.history:
            classes = "row current" if v.n == request.version.n else "row"
            when = _date(v.created_at) if v.created_at else ""
            rows.append(
                f'<li class="{classes}"><span class="n">v{v.n}</span>'
                f'<span class="when">{_esc(when)}</span>'
                f'<span class="size">{len(v.content)} chars</span></li>'
            )
        return (
            '<section class="history"><h2>Versions</h2>'
            f'<ul class="rows">{"".join(rows)}</ul></section>'
        )

    def _lineage(self, request: RenderRequest) -> str:
        lineage = request.lineage
        if lineage is None:
            return ""
        scope = request.scope
        sections: list[str] = []

        confirmed = [
            e
            for e in lineage.sources
            if e.state is EdgeState.CONFIRMED and scope.contains(e.to_artifact_id)
        ]
        if confirmed:
            sections.append(
                self._edge_list(
                    "Built on",
                    [
                        (
                            e.to_name or e.to_artifact_id,
                            f"v{e.pinned_n}",
                            (f"source now at v{e.source_head_n}" if e.is_stale else "up to date"),
                            e.is_stale,
                        )
                        for e in confirmed
                    ],
                )
            )

        if lineage.citations:
            items = "".join(
                f'<li class="row"><span class="name">{_esc(c.title or c.url)}</span>'
                f'<span class="when">{_esc(c.kind)}</span></li>'
                for c in lineage.citations
            )
            sections.append(
                f'<section class="lineage"><h2>External sources</h2>'
                f'<ul class="rows">{items}</ul></section>'
            )

        if scope.include_candidates and lineage.candidates:
            sections.append(
                self._edge_list(
                    "Captured, not confirmed",
                    [
                        (e.to_name or e.to_artifact_id, f"v{e.pinned_n}", "unreviewed", False)
                        for e in lineage.candidates
                        if scope.contains(e.to_artifact_id)
                    ],
                )
            )

        if scope.include_dependents:
            dependents = [e for e in lineage.dependents if scope.contains(e.from_artifact_id)]
            if dependents:
                sections.append(
                    self._edge_list(
                        "Depended on by",
                        [
                            (
                                e.from_name or e.from_artifact_id,
                                f"v{e.from_n}",
                                (f"pinned to v{e.pinned_n}" if e.pinned_n else ""),
                                e.is_stale,
                            )
                            for e in dependents
                        ],
                    )
                )
        return "".join(sections)

    @staticmethod
    def _edge_list(
        heading: str,
        rows: list[tuple[str, str, str, bool]],
    ) -> str:
        if not rows:
            return ""
        items = "".join(
            f'<li class="row{" stale" if flag else ""}">'
            f'<span class="name">{_esc(name)}</span>'
            f'<span class="n">{_esc(version)}</span>'
            f'<span class="note">{_esc(note)}</span></li>'
            for name, version, note, flag in rows
        )
        return (
            f'<section class="lineage"><h2>{_esc(heading)}</h2>'
            f'<ul class="rows">{items}</ul></section>'
        )

    @staticmethod
    def _stale_edges(request: RenderRequest) -> list[LineageEdge]:
        if request.lineage is None or not request.version.head:
            # A superseded version being out of date is a fact about
            # history, not something the reader can act on.
            return []
        return [
            e for e in request.lineage.stale_sources if request.scope.contains(e.to_artifact_id)
        ]


# ── Formatting helpers ────────────────────────────────────────────────


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _date(raw: str) -> str:
    """Trim an ISO timestamp to minutes — seconds are noise here."""
    text = str(raw or "")
    match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", text)
    return f"{match.group(1)} {match.group(2)}" if match else text[:16]


def _markdownish(content: str) -> str:
    """Render the small subset of Markdown artifacts actually use.

    A full Markdown library would be a dependency in the export path,
    and the export has to stay self-contained with no runtime. Headings,
    fenced code, lists and paragraphs cover what these documents are;
    anything else survives as escaped text rather than being mangled.
    """
    lines = str(content or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        if line.strip().startswith("```"):
            close_list()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(_esc(line))
            continue
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(bullet.group(1))}</li>")
            continue
        close_list()
        out.append(f"<p>{_inline(stripped)}</p>")

    close_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape first, then re-introduce the few inline marks we support."""
    escaped = _esc(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


# Both colour schemes ship because the page is read in a browser, a
# print dialog and whatever the recipient's OS is set to.
_CSS = """
:root { color-scheme: light dark;
  --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e4e4e7;
  --accent:#b45309; --warn-bg:#fff7ed; --warn-line:#fdba74; --code:#f6f6f7; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#141416; --fg:#ececee; --muted:#9a9aa2; --line:#2c2c31;
  --accent:#fbbf24; --warn-bg:#2a1f10; --warn-line:#7c4a12; --code:#1d1d21; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.65 ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
.page { max-width:46rem; margin:0 auto; padding:3rem 1.25rem 5rem; }
header h1 { font-size:1.9rem; line-height:1.2; margin:0 0 .4rem; }
.meta { color:var(--muted); margin:0 0 .15rem; font-size:.9rem; }
.id { color:var(--muted); margin:0 0 1.5rem; font-size:.78rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.banner { background:var(--warn-bg); border:1px solid var(--warn-line);
  border-radius:.5rem; padding:.85rem 1rem; margin:0 0 1.75rem; font-size:.92rem; }
.banner ul { margin:.5rem 0 0; padding-left:1.15rem; }
.content { border-top:1px solid var(--line); padding-top:1.75rem; }
.content h2 { font-size:1.3rem; margin:2rem 0 .6rem; }
.content h3 { font-size:1.08rem; margin:1.6rem 0 .5rem; }
.content p { margin:0 0 1rem; }
.content ul { margin:0 0 1rem; padding-left:1.3rem; }
pre { background:var(--code); border:1px solid var(--line); border-radius:.5rem;
  padding:.85rem 1rem; overflow-x:auto; font-size:.86rem; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; }
p code, li code { background:var(--code); padding:.1rem .3rem; border-radius:.25rem; }
.history, .lineage { margin-top:2.5rem; }
.history h2, .lineage h2 { font-size:.8rem; letter-spacing:.07em;
  text-transform:uppercase; color:var(--muted); margin:0 0 .6rem; }
.rows { list-style:none; margin:0; padding:0; }
.row { display:flex; gap:.85rem; align-items:baseline; padding:.45rem 0;
  border-bottom:1px solid var(--line); font-size:.92rem; }
.row .name { flex:1; }
.row .n { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.84rem; }
.row .when, .row .size, .row .note { color:var(--muted); font-size:.84rem; }
.row.current { font-weight:600; }
.row.stale .note { color:var(--accent); font-weight:600; }
@media print { .page { max-width:none; padding:0; }
  .banner { border:1px solid #999; } }
"""
