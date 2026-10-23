/**
 * Markdown with its block structure intact.
 *
 * The floor published exactly one markdown component and it was
 * `MarkdownInline`, which is *designed* to destroy block structure:
 * headings lose their size, lists become middots, images return
 * null. That is right for a two-line preview and exactly wrong for a
 * document, so an extension with prose in it bundled its own
 * renderer — and then had nothing to style it against, because
 * igni's prose rules live in `theme.css` and were not published
 * either.
 *
 * `examples/release-notes` hand-rolls a deliberately small subset
 * and says so. This is what that was missing.
 *
 * **Not a configurable pipeline.** No plugin prop, no component
 * overrides. The value here is that prose in an extension looks like
 * prose in igni, and every escape hatch is a way for that to stop
 * being true. An extension that needs a different pipeline should
 * bundle react-markdown directly — it is a peer dependency it
 * already has — and wrap the result in `.ui-prose`.
 */
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/**
 * Links open where they point, safely.
 *
 * A document can arrive from a colleague, so the href is somebody
 * else's. `noopener` is the one that matters: without it the opened
 * page gets a handle on this frame through `window.opener` and can
 * navigate it somewhere else.
 */
const COMPONENTS: Components = {
  a: ({ href, children, ...rest }) => (
    <a {...rest} href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

export interface MarkdownProps {
  /** The source. Rendered as a document, not flattened. */
  children: string;
  className?: string;
}

/**
 * Render markdown as prose.
 *
 * GitHub-flavoured, because that is what people write: tables,
 * strikethrough, task lists and autolinks. Raw HTML is **not**
 * enabled and will not be — `rehype-raw` in a surface that renders
 * text somebody else wrote is a script tag waiting to happen, and an
 * extension's frame has a CSP precisely so that it cannot.
 */
export function Markdown({ children, className }: MarkdownProps) {
  return (
    <div className={cx("ui-prose", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
