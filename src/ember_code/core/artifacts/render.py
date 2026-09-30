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
    """Everything the page needs, already fetched.

    No version history. An export is one version of a document —
    the one that was asked for — and a list of the others below the
    text was neither the document nor a way to reach them: it
    rendered as rows of "v3 · 12 March · 4820 chars", none of them a
    link, in a file that has no other version beside it to link to.
    Somebody sent the export to somebody else, and what arrived was
    the document with an inventory of drafts stapled underneath.

    Where the other versions genuinely are reachable — the app, the
    shared document page — there is a picker that knows how to fetch
    them, which is the thing this was a paper imitation of.
    """

    summary: ArtifactSummary
    version: ArtifactVersion
    lineage: ArtifactLineage | None = None
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


#: A fence, and the language written after it.
_FENCE = re.compile(r"^(?P<mark>```+|~~~+)[ \t]*(?P<info>[^\s`]*)")
#: ``- x``, ``* x``, ``+ x``, ``1. x``, ``1) x`` — with its indent.
_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?P<mark>[-*+]|\d{1,9}[.)])[ \t]+(?P<text>.*)$")
#: ``- [x] done`` / ``- [ ] not yet``.
_TASK = re.compile(r"^\[(?P<state>[ xX])\][ \t]+(?P<text>.*)$")
#: ``---``, ``***``, ``___`` on their own.
_RULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
#: The row under a table header: ``|---|:--:|``.
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(\|[ \t]*:?-{1,}:?[ \t]*)*\|?[ \t]*$")

#: How many spaces one level of list indentation is worth. Tabs are
#: expanded to this too, so a document written with either nests the
#: same way.
_INDENT = 2


def _markdownish(content: str) -> str:
    """Render the Markdown these documents are actually written in.

    A full Markdown library would be a dependency in the export path,
    and the export has to stay self-contained with no runtime — so
    this stays hand-written. What it covers is not a taste decision
    though: it is what turned up in real documents, and everything
    listed here was at some point rendered as literal text in an
    exported PDF somebody sent to somebody else.

    The worst of it was **paragraphs**. Markdown ends a paragraph at a
    blank line; this used to end one at every newline, so prose
    wrapped at eighty columns came out as one short paragraph per
    line, with a gap between each. Every document written the normal
    way looked shredded, and it looked like a font problem rather
    than a parser problem, which is why it survived so long.

    Kept deliberately small: no HTML passthrough, no reference links,
    no footnotes, no nested block quotes. Anything not handled ends up
    escaped in a paragraph, which is wrong in a way a reader can see
    and recover from rather than wrong in a way that eats the text.
    """
    lines = [
        line.replace("\t", " " * _INDENT)
        for line in str(content or "").replace("\r\n", "\n").split("\n")
    ]
    out: list[str] = []
    at = 0

    while at < len(lines):
        line = lines[at]
        stripped = line.strip()

        fence = _FENCE.match(stripped)
        if fence:
            at = _fenced_code(lines, at, fence, out)
            continue
        if not stripped:
            at += 1
            continue
        if _RULE.match(line):
            out.append("<hr>")
            at += 1
            continue
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*$", line)
        if heading:
            # One level down: the document's own title is the ``h1``
            # on the page, so its top-level headings are ``h2``.
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            at += 1
            continue
        if stripped.startswith(">"):
            at = _quote(lines, at, out)
            continue
        if _ITEM.match(line):
            at = _list(lines, at, out)
            continue
        if "|" in stripped and at + 1 < len(lines) and _TABLE_RULE.match(lines[at + 1]):
            at = _table(lines, at, out)
            continue
        at = _paragraph(lines, at, out)

    return "\n".join(out)


def _fenced_code(lines: list[str], at: int, fence: re.Match[str], out: list[str]) -> int:
    """A fenced block, ending at a fence of the same kind or the text.

    The info string is kept as ``language-x``. It used to be dropped,
    which mattered more than it sounds: a ``json-render`` fence is a
    panel in the app and raw JSON in an export, and without the
    language on it the reader was shown a wall of braces with nothing
    saying what it was or where it came from.
    """
    mark = fence.group("mark")[0] * 3
    info = fence.group("info")
    body: list[str] = []
    at += 1
    while at < len(lines):
        closing = _FENCE.match(lines[at].strip())
        if closing and closing.group("mark").startswith(mark) and not closing.group("info"):
            at += 1
            break
        body.append(_esc(lines[at]))
        at += 1
    opening = f'<pre><code class="language-{_esc(info)}">' if info else "<pre><code>"
    out.append(f"{opening}{chr(10).join(body)}</code></pre>")
    return at


def _quote(lines: list[str], at: int, out: list[str]) -> int:
    """A block quote, rendered by re-reading its contents as Markdown.

    Recursive because a quote holds blocks — a paragraph that wraps
    over three lines is one paragraph inside the quote too, and the
    rule that joins them is the one above.
    """
    inner: list[str] = []
    while at < len(lines) and lines[at].strip().startswith(">"):
        inner.append(re.sub(r"^[ \t]*>[ \t]?", "", lines[at]))
        at += 1
    out.append(f"<blockquote>{_markdownish(chr(10).join(inner))}</blockquote>")
    return at


def _list(lines: list[str], at: int, out: list[str]) -> int:
    """One list, however deeply it nests.

    Depth comes from indentation, held on a stack so that closing a
    level closes exactly the tags it opened. The flat version put
    every item at the top level, which turned a two-level checklist
    into one long undifferentiated list — legible, and not what was
    written.
    """
    # Each entry: (indent, tag).
    stack: list[tuple[int, str]] = []

    def close_to(indent: int) -> None:
        while stack and stack[-1][0] > indent:
            out.append(f"</{stack.pop()[1]}>")

    while at < len(lines):
        item = _ITEM.match(lines[at])
        if item is None:
            if lines[at].strip():
                break
            # A blank line inside a list is allowed, as long as the
            # list carries on after it.
            if at + 1 < len(lines) and _ITEM.match(lines[at + 1]):
                at += 1
                continue
            break

        indent = len(item.group("indent"))
        tag = "ol" if item.group("mark")[-1] in ".)" else "ul"
        close_to(indent)
        if not stack or stack[-1][0] < indent:
            stack.append((indent, tag))
            out.append(f"<{tag}>")
        elif stack[-1][1] != tag:
            # The marker changed at the same depth, which is a new
            # list rather than a continuation of this one.
            out.append(f"</{stack.pop()[1]}>")
            stack.append((indent, tag))
            out.append(f"<{tag}>")

        text = item.group("text")
        task = _TASK.match(text)
        if task:
            # Rendered rather than left as "[x]". Disabled because a
            # PDF is not somewhere a box can be ticked, and a control
            # that looks live and is not is worse than a mark.
            done = task.group("state").lower() == "x"
            box = f'<input type="checkbox" disabled{" checked" if done else ""}>'
            out.append(f'<li class="task">{box} {_inline(task.group("text"))}</li>')
        else:
            out.append(f"<li>{_inline(text)}</li>")
        at += 1

    close_to(-1)
    return at


def _table(lines: list[str], at: int, out: list[str]) -> int:
    """A pipe table. The header row, the rule, then the body."""

    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header = cells(lines[at])
    at += 2  # the header and the rule under it
    head = "".join(f"<th>{_inline(c)}</th>" for c in header)
    body: list[str] = []
    while at < len(lines) and "|" in lines[at] and lines[at].strip():
        row = cells(lines[at])
        # Padded to the header's width rather than dropped: a short
        # row is a typo in one cell, and losing the rest of it hides
        # the data instead of showing the gap.
        row += [""] * (len(header) - len(row))
        body.append(
            "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row[: len(header)]) + "</tr>"
        )
        at += 1
    rows = "".join(body)
    out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    return at


def _paragraph(lines: list[str], at: int, out: list[str]) -> int:
    """Consecutive lines, as one paragraph.

    The fix for the shredding. A paragraph runs to a blank line or to
    whatever starts the next block, and the lines in it are joined
    with a space — which is what a newline means inside a Markdown
    paragraph.
    """
    body: list[str] = []
    while at < len(lines):
        line = lines[at]
        stripped = line.strip()
        if not stripped or _FENCE.match(stripped) or _RULE.match(line):
            break
        if stripped.startswith(">") or _ITEM.match(line):
            break
        if re.match(r"^ {0,3}#{1,6}[ \t]", line):
            break
        body.append(stripped)
        at += 1
    if body:
        out.append(f"<p>{_inline(' '.join(body))}</p>")
    return at


#: A run of backticks, the shortest thing it can close around, and a
#: matching run. Written as one alternation so a longer run wins
#: before a shorter one gets a chance — ```` ``` ```` inside prose
#: used to be chewed into three separate code spans by a pattern that
#: only knew about single backticks.
_CODE_SPAN = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)(?!`)", re.DOTALL)
#: ``[text](url)``. The URL is checked before it is used — see
#: :func:`_link`.
_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<href>[^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)")
#: Schemes a link in a document may point at. Anything else keeps its
#: brackets and goes nowhere: an export is opened from disk and from
#: a mail client, and ``javascript:`` in a document that renders as a
#: page is a hole, not a feature.
_SAFE_SCHEMES = ("http://", "https://", "mailto:", "#", "/")


def _inline(text: str) -> str:
    """Escape first, then re-introduce the inline marks we support.

    Order matters. Code spans are taken out before anything else and
    put back afterwards, so ``**`` inside backticks stays two
    asterisks rather than turning the rest of the line bold.
    """
    escaped = _esc(text)

    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(f"<code>{match.group('body')}</code>")
        return f"\x00{len(spans) - 1}\x00"

    escaped = _CODE_SPAN.sub(stash, escaped)
    escaped = _LINK.sub(_link, escaped)
    escaped = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", escaped)
    escaped = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<strong>\1</strong>", escaped)
    # Single marks last, and only around non-space, so a lone
    # asterisk in prose and ``snake_case_names`` are left alone.
    escaped = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", r"<em>\1</em>", escaped)

    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], escaped)


def _link(match: re.Match[str]) -> str:
    """One link, or the text of one if its destination is not safe."""
    href = match.group("href")
    text = match.group("text") or href
    if not href.lower().startswith(_SAFE_SCHEMES):
        return f"[{text}]({href})"
    return f'<a href="{href}">{text}</a>'


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
.content h4 { font-size:.98rem; margin:1.4rem 0 .45rem; }
.content p { margin:0 0 1rem; }
.content ul, .content ol { margin:0 0 1rem; padding-left:1.3rem; }
.content li > ul, .content li > ol { margin:.25rem 0 .35rem; }
.content li { margin:.15rem 0; }
.content li.task { list-style:none; margin-left:-1.15rem; }
.content li.task input { margin-right:.4rem; }
.content a { color:var(--accent); }
.content hr { border:0; border-top:1px solid var(--line); margin:2rem 0; }
blockquote { margin:0 0 1rem; padding:.1rem 0 .1rem 1rem;
  border-left:3px solid var(--line); color:var(--muted); }
blockquote p:last-child { margin-bottom:0; }
table { border-collapse:collapse; width:100%; margin:0 0 1.25rem; font-size:.93rem;
  display:block; overflow-x:auto; }
th, td { border:1px solid var(--line); padding:.4rem .6rem; text-align:left;
  vertical-align:top; }
th { background:var(--code); font-weight:600; }
pre { background:var(--code); border:1px solid var(--line); border-radius:.5rem;
  padding:.85rem 1rem; overflow-x:auto; font-size:.86rem;
  /* A fenced block in a PDF cannot be scrolled, so it wraps. On
     screen the horizontal scroll above still applies to what is
     genuinely too wide. */
  white-space:pre-wrap; word-break:break-word; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; }
p code, li code, td code { background:var(--code); padding:.1rem .3rem; border-radius:.25rem; }
.lineage { margin-top:2.5rem; }
.lineage h2 { font-size:.8rem; letter-spacing:.07em;
  text-transform:uppercase; color:var(--muted); margin:0 0 .6rem; }
.rows { list-style:none; margin:0; padding:0; }
.row { display:flex; gap:.85rem; align-items:baseline; padding:.45rem 0;
  border-bottom:1px solid var(--line); font-size:.92rem; }
.row .name { flex:1; }
.row .n { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.84rem; }
.row .when, .row .note { color:var(--muted); font-size:.84rem; }
.row.stale .note { color:var(--accent); font-weight:600; }
@media print { .page { max-width:none; padding:0; }
  .banner { border:1px solid #999; }
  /* Keep a heading with what it introduces, and do not split a
     block across a page break where the two halves read as
     unrelated. */
  h1, h2, h3, h4 { break-after:avoid; }
  pre, blockquote, table, li { break-inside:avoid; }
  thead { display:table-header-group; } }
"""
