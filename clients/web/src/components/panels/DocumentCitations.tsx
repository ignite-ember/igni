import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { IgniClient } from "../../protocol/client";
import { MarkdownInline } from "@igni/ui-react";
import { findText } from "./DocumentComment";
import { hueFor, type CandidateOverlap } from "./ProvenanceView";

/**
 * Where a passage came from, marked in the passage.
 *
 * "Built on Rate limiting v3" sits under the article, and a reader who
 * wants to know whether *this sentence* rests on it has to leave the
 * text, read a list, and come back. The trace view answers properly
 * but is a mode — you stop reading the document to look at its
 * provenance, which is the one moment you were not asking about
 * provenance.
 *
 * So the citation goes where the citation is about. A superscript at
 * the end of each drawn-on passage, numbered in reading order,
 * coloured by source; clicking it says which document, which version
 * and which lines, and quotes them.
 *
 * **Nothing is inserted into the document.** The markers are an
 * overlay positioned from each passage's own geometry, not nodes
 * spliced into markdown React rendered — the same reason the comment
 * highlights use the Custom Highlight API. Text React owns and text
 * something else mutates is a reconciliation bug waiting for a
 * re-render.
 *
 * The honest limit is granularity, and it is visible: a citation can
 * only be as precise as the chunk it came from, and the chunker
 * produces coarse blocks. A marker against sixteen lines says "this
 * part of the document draws on that one", which is true and is less
 * than a citation usually promises.
 */

/** One source's measured overlap with this document. */
interface Evidence {
  source_name: string;
  source_n: number;
  overlaps: CandidateOverlap[];
  error: string;
}

/** A confirmed source, as the lineage reports it. */
export interface CitedSource {
  artifact_id: string;
  name: string;
  pinned_n: number;
}

/** A marker to draw: which passage, from where, and where it sits. */
interface Citation {
  key: string;
  number: number;
  sourceId: string;
  sourceName: string;
  sourceN: number;
  lineFrom: number;
  lineTo: number;
  quote: string;
  similarity: number;
  strength: string;
  hue: number;
  /** How far down the document body the passage ends, in px. */
  top: number;
  /** How far into the flattened text the passage ends — the sort key. */
  at: number;
}

/**
 * Markdown as the reader sees it.
 *
 * The passage text is a slice of the *source*, syntax and all; the
 * body it has to be found in is rendered, where the syntax is gone —
 * "## Reading your budget" on one side, "Reading your budget" on the
 * other. Matching the two verbatim finds nothing, every time, and the
 * failure is silent: no marker, no error, a feature that looks
 * switched off.
 *
 * Approximate on purpose. It only has to be close enough to locate
 * the passage, and a miss costs a marker rather than putting one in
 * the wrong place.
 */
export function asRendered(markdown: string): string {
  return String(markdown || "")
    .replace(/^```.*$/gm, " ")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")
    .replace(/^\s{0,3}\d+\.\s+/gm, "")
    .replace(/^\s*\|?[\s|:-]+\|\s*$/gm, " ")
    .replace(/\|/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_~]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** The first and last words of a passage: enough to span it. */
function ends(text: string, span = 60): [string, string] {
  const body = asRendered(text);
  if (body.length <= span * 2) return [body, body];
  const head = body.slice(0, span).replace(/\s\S*$/, "");
  const tail = body.slice(-span).replace(/^\S*\s/, "");
  return [head, tail];
}

/**
 * Find a passage in the rendered body, as a range spanning it.
 *
 * Matched at its two ends rather than whole. A passage is a chunk —
 * sixteen lines, a code fence, half a table — and one stripped
 * character anywhere in it loses the whole match. The first and last
 * few words are short enough to survive the approximation and are all
 * that is needed: one gives the range's start, the other its end.
 */
function locate(body: HTMLElement, text: string): Range | null {
  const [head, tail] = ends(text);
  const from = findText(body, head);
  const to = head === tail ? from : findText(body, tail);
  if (!from && !to) return null;
  if (!from || !to) return (from ?? to) as Range;
  try {
    const span = document.createRange();
    span.setStart(from.startContainer, from.startOffset);
    span.setEnd(to.endContainer, to.endOffset);
    // A tail found *before* the head is a mismatch, not a passage.
    return span.collapsed ? from : span;
  } catch {
    return from;
  }
}

/**
 * Which line a passage ends on, relative to an element.
 *
 * The line, not the exact character. A marker placed where the
 * passage actually ends lands *on top of* whatever follows it —
 * chunks are cut to a character budget, so a passage routinely ends
 * mid-sentence and the superscript sits over the next letter. Only
 * the vertical position is used; the marker goes in the margin.
 */
function endOf(range: Range, within: HTMLElement): { top: number } | null {
  const rects = range.getClientRects();
  const rect = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) return null;
  return { top: rect.top - within.getBoundingClientRect().top };
}

export function DocumentCitations({
  client,
  artifactId,
  sources,
  bodyRef,
  /** Changing this re-measures: the text moved under the markers. */
  version,
  onOpenSource,
}: {
  client: IgniClient;
  artifactId: string;
  sources: CitedSource[];
  bodyRef: React.RefObject<HTMLElement | null>;
  version: number;
  onOpenSource?: (id: string) => void;
}) {
  const [evidence, setEvidence] = useState<Map<string, Evidence>>(new Map());
  const [citations, setCitations] = useState<Citation[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  /** Lit while the pointer or keyboard focus is on a marker. */
  const [grazed, setGrazed] = useState<string | null>(null);
  const ranges = useRef(new Map<string, Range>());

  // Keyed by id so a re-render with the same sources does not refetch.
  const key = useMemo(
    () => sources.map((s) => `${s.artifact_id}:${s.pinned_n}`).join(","),
    [sources],
  );

  /**
   * The sources, reachable without depending on their identity.
   *
   * The parent rebuilds this array every render, so anything that
   * depends on it is rebuilt every render too, and the measurement
   * effect re-runs for no reason. ``key`` is the real dependency — it
   * is the same string until the sources actually change.
   */
  const latest = useRef(sources);
  latest.current = sources;

  useEffect(() => {
    if (!sources.length) {
      setEvidence(new Map());
      return;
    }
    let cancelled = false;
    // Cheap after the first time: the overlap is stored on the edge
    // and only recomputed when one of the two versions moves.
    void Promise.all(
      sources.map((s) =>
        client
          .rpc<Evidence>("artifact_candidate_evidence", {
            id: artifactId,
            source_id: s.artifact_id,
          })
          .then((r) => [s.artifact_id, r] as const)
          .catch(() => [s.artifact_id, null] as const),
      ),
    ).then((pairs) => {
      if (cancelled) return;
      const next = new Map<string, Evidence>();
      for (const [id, value] of pairs) {
        if (value && !value.error) next.set(id, value);
      }
      setEvidence(next);
    });
    return () => {
      cancelled = true;
    };
    // ``key`` stands in for ``sources``, which is a new array each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, artifactId, key]);

  /** Find every cited passage in the rendered text and place a marker. */
  const measure = useCallback(() => {
    const sources = latest.current;
    const body = bodyRef.current;
    if (!body || evidence.size === 0) {
      ranges.current = new Map();
      setCitations([]);
      return;
    }

    const found: Citation[] = [];
    const located = new Map<string, Range>();
    const names = sources.map((s) => s.name);

    for (const source of sources) {
      const measured = evidence.get(source.artifact_id);
      if (!measured) continue;
      for (const overlap of measured.overlaps) {
        const range = locate(body, overlap.here_text);
        if (!range) continue;
        const spot = endOf(range, body);
        if (!spot) continue;
        const probe = document.createRange();
        probe.selectNodeContents(body);
        probe.setEnd(range.endContainer, range.endOffset);
        const citationKey = `${source.artifact_id}:${overlap.here_line_from}`;
        located.set(citationKey, range);
        found.push({
          key: citationKey,
          number: 0,
          sourceId: source.artifact_id,
          sourceName: measured.source_name || source.name,
          sourceN: measured.source_n || source.pinned_n,
          lineFrom: overlap.there_line_from,
          lineTo: overlap.there_line_to,
          quote: overlap.there_text,
          similarity: overlap.similarity,
          strength: overlap.strength,
          hue: hueFor(names, source.name),
          top: spot.top,
          at: probe.toString().length,
        });
      }
    }

    // Numbered in reading order, the way a reader expects: the first
    // marker down the page is 1, whichever source it points at.
    found.sort((a, b) => a.at - b.at);
    found.forEach((citation, i) => {
      citation.number = i + 1;
    });

    ranges.current = located;
    setCitations(found);
  }, [bodyRef, evidence]);

  /**
   * Re-measured whenever the text could have moved: a new version, a
   * reflow, a font that arrived late. Markers pinned to stale
   * geometry are worse than none — they point confidently at the
   * wrong line.
   *
   * A *layout* effect, and not a ``requestAnimationFrame``. The frame
   * seemed like the right place — measure after the markdown is laid
   * out — but Chrome does not run animation frames in a background
   * tab, so a document opened in one had no markers at all until it
   * was focused, with nothing on screen or in the console to say why.
   * A layout effect runs on commit whether or not anything is being
   * painted, and ``getBoundingClientRect`` forces the layout it needs.
   *
   * ``ResizeObserver`` covers the rest: it fires when the body's box
   * changes, which is what a late font or a window resize does.
   */
  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    measure();
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => measure());
    observer?.observe(body);
    return () => observer?.disconnect();
  }, [measure, bodyRef, version]);

  /**
   * Light the cited passage while a marker is pointed at or open.
   *
   * On hover, not only on click. The marker sits in the margin and a
   * chunk can run to sixteen lines, so where the citation *starts* is
   * the one thing its position cannot say — and answering that by
   * opening a card makes the reader commit to a detour to learn how
   * much text is involved. Grazing the marker answers it for free and
   * costs nothing to undo.
   *
   * Focus counts as grazing, so tabbing through the markers previews
   * them the same way.
   *
   * The grazed one wins over the open one: with a card open, moving
   * along the margin should preview the others rather than keep
   * lighting the passage whose card you already have.
   */
  useEffect(() => {
    const highlights =
      typeof CSS === "undefined"
        ? undefined
        : (CSS as unknown as { highlights?: Map<string, unknown> }).highlights;
    const HighlightCtor = (
      window as unknown as { Highlight?: new (...r: Range[]) => unknown }
    ).Highlight;
    if (!highlights || !HighlightCtor) return;
    const which = grazed ?? open;
    const lit = which ? ranges.current.get(which) : undefined;
    if (lit) highlights.set("doc-cite-lit", new HighlightCtor(lit));
    else highlights.delete("doc-cite-lit");
    return () => {
      highlights.delete("doc-cite-lit");
    };
  }, [open, grazed]);

  // Escape closes, capture phase so the page behind does not also
  // navigate back on the same keypress.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(null);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open]);

  if (!citations.length) return null;
  const shown = citations.find((c) => c.key === open) ?? null;

  return (
    <div className="doc-cites" aria-hidden={false}>
      {citations.map((citation) => (
        <button
          key={citation.key}
          type="button"
          className={open === citation.key ? "doc-cite doc-cite-open" : "doc-cite"}
          style={
            {
              top: `${citation.top}px`,
              "--cite-hue": String(citation.hue),
            } as React.CSSProperties
          }
          title={`Draws on ${citation.sourceName} v${citation.sourceN}`}
          aria-label={`Citation ${citation.number}: ${citation.sourceName} v${citation.sourceN}`}
          onMouseEnter={() => setGrazed(citation.key)}
          onMouseLeave={() => setGrazed((on) => (on === citation.key ? null : on))}
          onFocus={() => setGrazed(citation.key)}
          onBlur={() => setGrazed((on) => (on === citation.key ? null : on))}
          onClick={() => setOpen((on) => (on === citation.key ? null : citation.key))}
        >
          {citation.number}
        </button>
      ))}

      {shown && (
        <>
          <div className="doc-cite-backdrop" onClick={() => setOpen(null)} />
          <div
            className="doc-cite-card"
            style={
              {
                top: `${shown.top + 20}px`,
                "--cite-hue": String(shown.hue),
              } as React.CSSProperties
            }
            role="dialog"
            aria-label={`Citation ${shown.number}`}
          >
            <div className="doc-cite-head">
              <span className="doc-cite-number">{shown.number}</span>
              {onOpenSource ? (
                <button
                  type="button"
                  className="doc-cite-source"
                  onClick={() => onOpenSource(shown.sourceId)}
                >
                  {shown.sourceName}
                </button>
              ) : (
                <strong className="doc-cite-source">{shown.sourceName}</strong>
              )}
              <span className="doc-cite-where">
                v{shown.sourceN} · L{shown.lineFrom}
                {shown.lineTo !== shown.lineFrom ? `–${shown.lineTo}` : ""}
              </span>
            </div>
            {/* Rendered, not raw. A quote of markdown printed
                literally is a wall of backticks and hashes where the
                reader is trying to judge prose. */}
            <blockquote className="doc-cite-quote">
              <MarkdownInline text={shown.quote} limit={600} />
            </blockquote>
            {/* Said on every card. These are resemblances between
                passages, not a record of what the author read — a line
                drawn with the confidence of a recorded fact would be a
                lie about what is known. */}
            <p className="doc-cite-foot">
              {shown.strength} match · {shown.similarity.toFixed(2)} — matched by meaning,
              not recorded when this was written.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
