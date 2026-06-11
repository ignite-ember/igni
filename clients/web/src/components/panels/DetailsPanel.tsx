import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { EmberClient } from "../../protocol/client";
import { Drawer } from "./Drawer";

type Json = Record<string, unknown>;

const NAME_KEYS = ["name", "title", "id", "server", "event"];
const DESC_KEYS = [
  "description",
  "desc",
  "summary",
  "status",
  "source",
  "model",
  "command",
  "path",
];

function pick(obj: Json, keys: string[]): string {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === "string" && v) return v;
  }
  return "";
}

function Row({ entry }: { entry: Json }) {
  const name = pick(entry, NAME_KEYS) || JSON.stringify(entry).slice(0, 60);
  const desc = pick(entry, DESC_KEYS);
  const flags = Object.entries(entry)
    .filter(([, v]) => typeof v === "boolean" && v)
    .map(([k]) => k)
    .join(" · ");
  return (
    <div className="row">
      <div>
        <div className="name">{name}</div>
        {(desc || flags) && <div className="meta">{[desc, flags].filter(Boolean).join(" · ")}</div>}
      </div>
    </div>
  );
}

/**
 * Structured panel for agents / skills / plugins / knowledge / hooks.
 * The TUI builds bespoke widgets from the `get_*_details` RPCs; this
 * renders the same data generically: arrays become rows, dicts become
 * key-value sections (with nested arrays as row lists).
 */
export function DetailsPanel({
  client,
  title,
  method,
  fallbackMarkdown,
  onClose,
}: {
  client: EmberClient;
  title: string;
  method: string;
  fallbackMarkdown?: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<unknown>(undefined);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    client
      .rpc(method)
      .then(setData)
      .catch(() => setFailed(true));
  }, [client, method]);

  let body;
  if (failed) {
    body = fallbackMarkdown ? (
      <div className="msg-assistant">
        <ReactMarkdown>{fallbackMarkdown}</ReactMarkdown>
      </div>
    ) : (
      <div className="msg-info">Unavailable.</div>
    );
  } else if (data === undefined) {
    body = <div className="msg-info">Loading…</div>;
  } else if (Array.isArray(data)) {
    body = data.length ? (
      data.map((e, i) => <Row key={i} entry={e as Json} />)
    ) : (
      <div className="msg-info">Nothing here yet.</div>
    );
  } else if (data && typeof data === "object") {
    const obj = data as Json;
    const sections: React.ReactNode[] = [];
    const scalars: [string, string][] = [];
    for (const [k, v] of Object.entries(obj)) {
      if (Array.isArray(v)) {
        sections.push(
          <div key={k}>
            <div className="sidebar-section" style={{ padding: "12px 0 4px" }}>
              {k.replace(/_/g, " ")}
            </div>
            {v.length ? (
              v.map((e, i) =>
                e && typeof e === "object" ? (
                  <Row key={i} entry={e as Json} />
                ) : (
                  <div key={i} className="row">
                    <div className="name">{String(e)}</div>
                  </div>
                ),
              )
            ) : (
              <div className="msg-info">none</div>
            )}
          </div>,
        );
      } else if (v !== null && typeof v !== "object") {
        scalars.push([k, String(v)]);
      }
    }
    body = (
      <>
        {scalars.length > 0 && (
          <dl className="kv">
            {scalars.map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k.replace(/_/g, " ")}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        )}
        {sections}
      </>
    );
  } else {
    body = <div className="msg-info">{String(data)}</div>;
  }

  return (
    <Drawer title={title} onClose={onClose}>
      {body}
    </Drawer>
  );
}
