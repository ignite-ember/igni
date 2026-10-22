import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Where each passage of a document came from, shown beside it.
 *
 * The lineage panel says "built on Auth model v1". That is the
 * document-level fact, and it is not the one somebody has when a
 * source moves: they want to know *which paragraph* is now out of
 * step, not whether any of them is.
 *
 * The layout is an **aligned sidebar**: the document on the left, the
 * source it draws on in a column to the right, each card sitting on
 * the same grid row as the passage it belongs to. That is not a taste
 * call. Comparing footer, hover-card and aligned-sidebar presentations
 * of the same citations, the sidebar was the only one to score well on
 * both counts at once — a footer is visible but nobody goes to it, a
 * hover card is reachable but nobody knows it is there — and it drew
 * more source interactions than either. (arXiv 2512.12207, "Not All
 * Transparency Is Equal".)
 *
 * Two more things follow from the same literature:
 *
 * - **Point at passages, not documents.** A citation that resolves to
 *   "somewhere in Auth model" is the thing this view exists to stop
 *   being the answer.
 * - **Say what is missing.** A gap left silent reads as "nothing to
 *   see"; named, it reads as a limit. So untraced passages are marked
 *   as untraced rather than left blank.
 *
 * Everything here is a *resemblance*, not a record — nothing stores
 * which passage came from which. The wording says so, because a line
 * drawn with the confidence of a recorded fact would be a lie about
 * what is known.
 */

/** One measured resemblance between a passage here and one there.
 *  Shared with the inline citations, which draw the same data in the
 *  reading view rather than in a mode of its own. */
export interface CandidateOverlap {
  here_text: string;
  here_line_from: number;
  here_line_to: number;
  there_text: string;
  there_line_from: number;
  there_line_to: number;
  similarity: number;
  strength: string;
}

export interface ProvenanceMatch {
  source_artifact_id: string;
  source_name: string;
  source_n: number;
  source_line_from: number;
  source_line_to: number;
  source_text: string;
  similarity: number;
  /** The same number in words, banded by the backend. */
  strength: string;
  changed: boolean;
}

export interface ProvenancePassage {
  chunk_id: string;
  line_from: number;
  line_to: number;
  text: string;
  matches: ProvenanceMatch[];
  affected: boolean;
}

export interface Provenance {
  artifact_id: string;
  n: number;
  passages: ProvenancePassage[];
  sources: string[];
  affected_count: number;
  inferred: boolean;
  unplaced: number;
  error: string;
}

/**
 * How sure, as the reader sees it: the word and the number together.
 *
 * The word arrives from the backend rather than being computed here.
 * Where the bands fall is an argument about what the measurements in
 * ``core/artifacts/provenance.py`` support — 0.74 because that is the
 * weakest derivation ever measured against this store — and an
 * argument kept in two places is one copy waiting to go stale. This
 * file had the stale copy: it still cut "close" at 0.80, which put
 * every real match measured into the weakest band and called it
 * "loose".
 *
 * The number is shown, not hidden in a tooltip. "close" is an opinion;
 * 0.78 is what the opinion was formed from, and a reader comparing two
 * candidates needs the second thing.
 */
export function strengthLabel(match: ProvenanceMatch): string {
  const score = Number.isFinite(match.similarity) ? match.similarity.toFixed(2) : "";
  if (!match.strength) return score;
  return score ? `${match.strength} · ${score}` : match.strength;
}

/**
 * A stable colour per source, so a card and its marker read as the
 * same source at a glance. Hue only — two sources differing in
 * lightness on a 3px marker are one source.
 */
export function hueFor(sources: string[], name: string): number {
  const at = sources.indexOf(name);
  if (at < 0) return 210;
  // Spread around the wheel rather than stepping, so two sources are
  // far apart even when there are only two.
  return (210 + at * 137) % 360;
}

/** A fence line: ``` or ~~~, indented or not. */
const FENCE = /^\s*(?:```|~~~)/gm;

/**
 * Make one slice of a document render as the markdown it is part of.
 *
 * A slice does not respect fences, and it can fall on either side of
 * one: end after opening a block, or begin inside a block it never
 * opened. The first case wants a fence appended, the second wants one
 * *prepended*, and the version of this that only counted fences did
 * the former in both cases.
 *
 * That was not a cosmetic miss. A slice beginning inside a ```yaml
 * block got a second closing fence, so the YAML was parsed as prose:
 * ``access_ttl_minutes`` came out as "access ttl minutes", because
 * markdown read the underscores as emphasis and ate them. Config that
 * silently loses characters is worse than config shown as plain text.
 *
 * ``insideAtStart`` cannot be worked out from the slice — ``` alone
 * is both an opening and a closing fence — so the caller, which has
 * the document, decides.
 */
export function balanceFences(text: string, insideAtStart = false): string {
  const body = insideAtStart ? `\`\`\`\n${text}` : text;
  const fences = (body.match(FENCE) ?? []).length;
  return fences % 2 === 1 ? `${body}\n\`\`\`` : body;
}

/** Whether line ``at`` (1-based) falls inside a fenced block. */
export function insideFenceAt(lines: string[], at: number): boolean {
  let open = false;
  for (const line of lines.slice(0, Math.max(0, at - 1))) {
    if (/^\s*(?:```|~~~)/.test(line)) open = !open;
  }
  return open;
}

/**
 * The header rows a slice needs to keep being a table.
 *
 * The other half of the same problem as a split code fence, and it
 * looks worse: a GFM table without its header and delimiter is not a
 * broken table, it is not a table at all, so the rows come out as
 * literal text — "| writes | 10 / min | 120 / min |" across the page.
 *
 * Returns the two lines to put back, or nothing when the slice does
 * not begin inside a table.
 */
export function tableHeadFor(lines: string[], at: number): string[] {
  const isRow = (line: string) => line.includes("|") && line.trim() !== "";
  const isDelimiter = (line: string) =>
    line.includes("|") && line.includes("-") && /^[\s|:-]+$/.test(line);

  let i = at - 2;
  if (i < 0 || !isRow(lines[i])) return [];
  while (i >= 0 && isRow(lines[i])) i--;

  const head = i + 1;
  const delimiter = lines[head + 1];
  // Nothing to restore if the slice starts at or above the delimiter:
  // it already carries its own header.
  if (delimiter === undefined || !isDelimiter(delimiter) || at - 1 <= head + 1) return [];
  return [lines[head], delimiter];
}

/** One row of the view: the document's own lines, and what they resemble. */
export interface LaidOutPassage {
  chunk_id: string;
  /** Whole lines of the document, ready to render. */
  markdown: string;
  matches: ProvenanceMatch[];
  affected: boolean;
  line_from: number;
  line_to: number;
}

const matchKey = (m: ProvenanceMatch) =>
  `${m.source_artifact_id}:${m.source_line_from}:${m.source_line_to}`;

/**
 * Lay the passages out as the document, read once.
 *
 * Chunks are an *embedding* artefact: they overlap on purpose, so a
 * sentence near a boundary is seen whole by at least one of them, and
 * they are cut to a character budget rather than to lines. Shown raw
 * that reads as damage — the live document produced passages starting
 * "ores", "on:" and "tion", with two paragraphs printed three times
 * between them.
 *
 * So the left column is rebuilt from the document itself, sliced on
 * the line numbers each chunk already carries and advanced past what
 * earlier rows have shown. Whole lines, nothing twice, and markdown
 * that parses. The chunk text is what the *embedding* saw and is
 * untouched by this; what changes is only what the reader is given to
 * read.
 *
 * A chunk whose lines were all covered by earlier rows has nothing
 * left to show. It is not dropped — its matches fold into the row
 * that did show those lines, because a citation lost to a layout
 * decision is the one failure this view cannot have.
 */
export function layOutPassages(
  passages: ProvenancePassage[],
  content: string,
): LaidOutPassage[] {
  // Without the document there is nothing to slice, so fall back to
  // the chunk text — mid-word starts and all. Better a rough render
  // than an empty view.
  if (!content.trim()) {
    return passages.map((p) => ({
      chunk_id: p.chunk_id,
      markdown: balanceFences(p.text),
      matches: p.matches,
      affected: p.affected,
      line_from: p.line_from,
      line_to: p.line_to,
    }));
  }

  const lines = content.split("\n");
  const rows: LaidOutPassage[] = [];
  let next = 1;

  for (const passage of passages) {
    const to = Math.min(passage.line_to, lines.length);
    if (to < next) {
      // Every line of this chunk is already on screen above.
      const host = rows[rows.length - 1];
      if (host) {
        const seen = new Set(host.matches.map(matchKey));
        host.matches = [
          ...host.matches,
          ...passage.matches.filter((m) => !seen.has(matchKey(m))),
        ];
        host.affected = host.affected || passage.affected;
      }
      continue;
    }

    // From ``next``, not from the chunk's own start: where chunks
    // overlap this trims the repeat, and in the gap left by a chunk
    // that failed to index it keeps the document unbroken rather
    // than quietly dropping the lines nobody claimed.
    const from = next;
    const inFence = insideFenceAt(lines, from);
    // Inside a fence a pipe is just a pipe, so the table repair is
    // only asked for when the slice is in prose.
    const head = inFence ? [] : tableHeadFor(lines, from);
    rows.push({
      chunk_id: passage.chunk_id,
      markdown: balanceFences(
        [...head, lines.slice(from - 1, to).join("\n")].join("\n"),
        inFence,
      ),
      matches: passage.matches,
      affected: passage.affected,
      line_from: from,
      line_to: to,
    });
    next = to + 1;
  }

  // Anything past the last indexed chunk still belongs to the reader.
  if (next <= lines.length && rows.length) {
    const tail = lines.slice(next - 1).join("\n");
    if (tail.trim()) {
      rows.push({
        chunk_id: `${rows[rows.length - 1].chunk_id}:tail`,
        markdown: balanceFences(
          [
            ...(insideFenceAt(lines, next) ? [] : tableHeadFor(lines, next)),
            tail,
          ].join("\n"),
          insideFenceAt(lines, next),
        ),
        matches: [],
        affected: false,
        line_from: next,
        line_to: lines.length,
      });
    }
  }

  return rows;
}

export function ProvenanceView({
  provenance,
  content = "",
  onOpenSource,
}: {
  provenance: Provenance;
  /** The document as written, so passages can be shown as its lines. */
  content?: string;
  /** Open a source document at the version being compared against. */
  onOpenSource?: (artifactId: string) => void;
}) {
  // Which pair is lit. Hovering either half lights both, so the line
  // between a passage and its source never has to be drawn.
  const [linked, setLinked] = useState<string | null>(null);

  const rows = useMemo(
    () => layOutPassages(provenance.passages, content),
    [provenance.passages, content],
  );

  const traced = useMemo(() => rows.filter((r) => r.matches.length > 0), [rows]);

  // How many passages each source accounts for — the legend's job is
  // to say which source carries the document, not merely name them.
  const perSource = useMemo(() => {
    const counts = new Map<string, { total: number; affected: number }>();
    for (const row of rows) {
      const best = row.matches[0];
      if (!best) continue;
      const seen = counts.get(best.source_name) ?? { total: 0, affected: 0 };
      seen.total += 1;
      if (row.affected) seen.affected += 1;
      counts.set(best.source_name, seen);
    }
    return counts;
  }, [rows]);

  if (provenance.error) return <div className="msg-error">{provenance.error}</div>;

  if (!provenance.passages.length) {
    return <div className="kb-hint">Nothing to trace — this version has no passages.</div>;
  }

  // Numbered across the document, the way a reader expects a citation
  // to be numbered: first mention is 1.
  const numbers = new Map<string, number>();
  traced.forEach((r, i) => numbers.set(r.chunk_id, i + 1));

  return (
    <div className="prov">
      <header className="prov-legend">
        <div className="prov-legend-sources">
          {perSource.size === 0 ? (
            <span className="prov-legend-empty">
              Nothing here resembles a confirmed source closely enough to trace.
            </span>
          ) : (
            Array.from(perSource.entries()).map(([name, row]) => (
              <span
                key={name}
                className="prov-chip"
                style={{ "--prov-hue": String(hueFor(provenance.sources, name)) } as React.CSSProperties}
              >
                <span className="prov-chip-dot" aria-hidden="true" />
                <span className="prov-chip-name">{name}</span>
                <span className="prov-chip-count">
                  {row.total} passage{row.total === 1 ? "" : "s"}
                </span>
                {row.affected > 0 && (
                  <span className="prov-chip-affected">{row.affected} affected</span>
                )}
              </span>
            ))
          )}
        </div>

        {/* Said once. Hedging every card would turn the caveat into
            noise and stop it being read at all. */}
        <p className="prov-caveat">
          Matched by meaning, not recorded when the document was written — treat each as a
          resemblance worth checking.
        </p>

        {provenance.unplaced > 0 && (
          <p className="prov-pending">
            {provenance.unplaced} passage{provenance.unplaced === 1 ? "" : "s"} not indexed
            yet — reopen this in a moment for the rest.
          </p>
        )}
      </header>

      <div className="prov-grid">
        {rows.map((passage) => {
          const best = passage.matches[0];
          const others = passage.matches.slice(1);
          const lit = linked === passage.chunk_id;
          const number = numbers.get(passage.chunk_id);
          const hue = best ? hueFor(provenance.sources, best.source_name) : 0;

          return (
            <div
              key={passage.chunk_id}
              className={[
                "prov-row",
                best ? "prov-traced" : "prov-untraced",
                passage.affected ? "prov-affected" : "",
                lit ? "prov-lit" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{ "--prov-hue": String(hue) } as React.CSSProperties}
              onMouseEnter={() => best && setLinked(passage.chunk_id)}
              onMouseLeave={() => setLinked(null)}
            >
              <div className="prov-passage markdown-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {passage.markdown}
                </ReactMarkdown>
                {best && (
                  <sup className="prov-marker" aria-hidden="true">
                    {number}
                  </sup>
                )}
              </div>

              <div className="prov-aside">
                {best ? (
                  <article
                    className="prov-card"
                    onFocus={() => setLinked(passage.chunk_id)}
                    onBlur={() => setLinked(null)}
                  >
                    <div className="prov-card-head">
                      <span className="prov-card-number" aria-hidden="true">
                        {number}
                      </span>
                      {onOpenSource ? (
                        <button
                          type="button"
                          className="prov-card-source"
                          onClick={() => onOpenSource(best.source_artifact_id)}
                          title={`Open ${best.source_name}`}
                        >
                          {best.source_name}
                        </button>
                      ) : (
                        <strong className="prov-card-source">{best.source_name}</strong>
                      )}
                      <span className="prov-card-where">
                        v{best.source_n} · L{best.source_line_from}
                        {best.source_line_to !== best.source_line_from
                          ? `–${best.source_line_to}`
                          : ""}
                      </span>
                    </div>

                    {/* Rendered, not shown raw. The quote is a slice of
                        somebody's document, and a slice of markdown
                        printed literally is a wall of backticks — the
                        card read "``` RateLimit-Limit: 120 ```" where
                        the source has a code block. The backend sends
                        whole lines with any cut fence repaired, so
                        there is nothing left to guess at here. */}
                    <blockquote className="prov-card-quote">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {best.source_text}
                      </ReactMarkdown>
                    </blockquote>

                    <div className="prov-card-foot">
                      <span
                        className="prov-strength"
                        title="Cosine similarity between the two passages' embeddings"
                      >
                        {strengthLabel(best)}
                      </span>
                      {passage.affected && (
                        <span className="prov-card-changed">changed since pinned</span>
                      )}
                    </div>

                    {/* Named rather than hidden. Showing only the top
                        match makes a guess look like a finding — the
                        documented failure of inline citation, which
                        converges on one source prematurely. */}
                    {others.length > 0 && (
                      <details className="prov-card-others">
                        <summary>
                          also resembles {others.length} other passage
                          {others.length === 1 ? "" : "s"}
                        </summary>
                        {others.map((m) => (
                          <div
                            key={`${m.source_artifact_id}:${m.source_line_from}:${m.source_line_to}`}
                            className="prov-other"
                          >
                            <span className="prov-card-where">
                              {m.source_name} v{m.source_n} · L{m.source_line_from}
                              {" · "}
                              {strengthLabel(m)}
                            </span>
                            <blockquote className="prov-card-quote">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                {m.source_text}
                              </ReactMarkdown>
                            </blockquote>
                          </div>
                        ))}
                      </details>
                    )}
                  </article>
                ) : (
                  /* A gap left blank reads as "nothing to see". Named,
                     it reads as a limit of the method. */
                  <p className="prov-none">not traced</p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
