/**
 * Mermaid diagrams, and the helper that spots a fenced block.
 *
 * ```mermaid``` becomes a diagram, in a document exactly as it
 * already does in chat.
 *
 * **A fence, deliberately.** This is what lets a document carry a
 * diagram while staying markdown: the source is a code block, so it
 * diffs a line at a time, chunks for search, survives the editor's
 * round trip untouched, and reads as its own source in any other
 * markdown tool. The alternative — embedding HTML, or a bespoke
 * node type — would cost every one of those.
 *
 * Shared by the chat and the document view rather than copied.
 * There was one mermaid renderer living inside ``ChatItems`` and a
 * document could not reach it, which is the usual way two renderers
 * of the same thing start disagreeing about what it looks like.
 *
 * **`mermaid` is a peer dependency**, loaded on demand. An
 * extension that never renders a diagram never downloads it; one
 * that does must have it installed. This is the only export under
 * the floor that asks anything of your package.json.
 *
 * ``SpecBlock`` used to live here and does not any more: it reaches
 * into the app for the ``json-render`` renderer, so it could not be
 * published without publishing that too. It is app code now.
 */
import type { ReactNode } from "react";
import { isValidElement, useEffect, useRef, useState } from "react";

type MermaidLib = {
  initialize: (opts: Record<string, unknown>) => void;
  render: (id: string, src: string) => Promise<{ svg: string }>;
};

let mermaidPromise: Promise<MermaidLib> | null = null;
let mermaidId = 0;

/**
 * A mermaid theme matching igni's.
 *
 * ``default`` is the colourful light palette and ``dark`` its dark
 * counterpart. Both run in colour — ``neutral``, which reads like
 * the safe choice, is what made every diagram grey.
 */
function mermaidTheme(): "default" | "dark" {
  if (typeof document === "undefined") return "default";
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "dark") return "dark";
  if (explicit === "light") return "default";
  if (typeof window !== "undefined" && window.matchMedia) {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "default";
  }
  return "default";
}

/** Mermaid is about 700KB, so it arrives only if a document has one. */
export function loadMermaid(): Promise<MermaidLib> {
  if (!mermaidPromise) {
    mermaidPromise = import("mermaid").then((m) => {
      const lib = m.default as unknown as MermaidLib;
      lib.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: mermaidTheme(),
        themeVariables: {
          fontFamily: "var(--font-ui)",
          background: "transparent",
        },
      });
      return lib;
    });
  }
  return mermaidPromise;
}

export function MermaidBlock({ source }: { source: string }) {
  const frame = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    void loadMermaid()
      .then((lib) => lib.render(`mermaid-${++mermaidId}`, source))
      .then(({ svg }) => {
        if (cancelled || !frame.current) return;
        frame.current.innerHTML = svg;
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [source]);

  if (error) {
    // The source, not a blank box. A diagram that does not parse is
    // usually a diagram somebody is halfway through writing, and
    // hiding what they wrote is the least helpful response to that.
    return (
      <pre className="mermaid-error" title={`Mermaid render failed: ${error}`}>
        {source}
      </pre>
    );
  }
  return (
    <div className="mermaid-diagram">
      <CopySource source={source} />
      <div ref={frame} />
    </div>
  );
}

/**
 * Copies the fenced source, not the rendered SVG.
 *
 * What somebody wants from a diagram is the text that made it —
 * to paste into another document, or to fix. Nobody wants the
 * markup a renderer produced.
 */
function CopySource({ source }: { source: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="diagram-copy"
      title={copied ? "Copied" : "Copy source"}
      aria-label={copied ? "Copied" : "Copy source"}
      onClick={() => {
        void (async () => {
          try {
            await navigator.clipboard.writeText(source);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1300);
          } catch {
            /* clipboard blocked — nothing worth saying about it */
          }
        })();
      }}
    >
      {copied ? "✓" : "⧉"}
    </button>
  );
}

/**
 * The language on a fenced block, or null.
 *
 * ReactMarkdown hands a ``<pre>`` a single ``<code>`` child carrying
 * ``className="language-mermaid"``; this reads that without
 * assuming any particular child shape survives a plugin.
 */
export function fenceInfo(
  children: ReactNode,
): { lang: string; source: string } | null {
  const only = Array.isArray(children) ? children[0] : children;
  if (!isValidElement(only)) return null;
  const props = only.props as { className?: string; children?: ReactNode };
  const match = /\blanguage-([\w-]+)\b/.exec(props.className ?? "");
  if (!match) return null;
  const source = typeof props.children === "string" ? props.children : "";
  return { lang: match[1].toLowerCase(), source: source.replace(/\n$/, "") };
}
