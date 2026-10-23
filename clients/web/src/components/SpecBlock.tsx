/**
 * A ``json-render`` spec, drawn inside a document or a chat message.
 *
 * App code, not floor code. It loads ``JsonRenderView`` from this
 * directory, so it cannot be published to an extension without
 * publishing the whole renderer — which is why it sits here and its
 * sibling ``MermaidBlock`` sits in ``ui-react``.
 */
import { useEffect, useMemo, useState } from "react";

import { parsePartialSpec } from "../chat/visualizationStream";

/**
 * A ``json-render`` spec, drawn.
 *
 * Checked before it is drawn, with the same guard the streaming
 * path uses: the renderer throws when a spec's root or children do
 * not resolve, and a throw inside a document takes the page with
 * it. A document can arrive from a colleague, so "this spec is
 * well-formed" is not an assumption available here.
 */
export function SpecBlock({ source }: { source: string }) {
  const spec = useMemo(() => parsePartialSpec(source), [source]);
  const [View, setView] = useState<null | React.ComponentType<{
    spec: unknown;
  }>>(null);

  // Loaded on demand, like mermaid: a document with no view in it
  // should not pay for the renderer.
  useEffect(() => {
    if (!spec) return;
    let cancelled = false;
    void import("../components/JsonRenderView").then((m) => {
      if (!cancelled) {
        setView(
          () =>
            m.JsonRenderView as unknown as React.ComponentType<{
              spec: unknown;
            }>,
        );
      }
    });
    return () => {
      cancelled = true;
    };
  }, [spec]);

  if (!spec) {
    return (
      <pre
        className="mermaid-error"
        title="This is not a renderable json-render spec"
      >
        {source}
      </pre>
    );
  }
  if (!View) return <pre className="doc-spec-loading">{source}</pre>;
  return (
    <div className="doc-spec">
      <View spec={spec} />
    </div>
  );
}
