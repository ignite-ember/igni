import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { IgniClient } from "../../protocol/client";
import { PeoplePicker } from "./PeoplePicker";

/**
 * Choose who can open a document, and publish it.
 *
 * **A popover, not a page.** It was a view: picking Share replaced
 * the document with a form and put "Share Release checklist" in the
 * breadcrumbs, so deciding who could read a paragraph meant leaving
 * the paragraph. Everything this asks is *about* the document on
 * screen — which makes reading the answer beside it, rather than
 * instead of it, the whole point.
 *
 * Anchored to whatever opened it, which is either the three-dot menu
 * or the line under the title that says who can see this today.
 * Portalled and fixed for the reason every other popup in this panel
 * is: the header sits in a column that scrolls, and a panel clipped
 * to that column is a panel with its buttons cut off.
 *
 * This is where `domain` and `public` become reachable by a person.
 * Until this existed they could only be set by a tool call, which
 * meant the widest two audiences in the product were the two hardest
 * to pick — exactly backwards for a decision this consequential.
 *
 * The reach line under each option is written out in full rather than
 * left to the label. "Domain" tells you nothing; "anyone with an
 * @acme.co address" tells you what you are about to do, and this is a
 * one-click action that can put a document in front of a company.
 *
 * There is no "my organisation" option. Every account gets an org
 * created for it at sign-up, so for most people that option shared
 * with nobody — while sitting directly beneath one that shared with
 * the whole company. A control whose failure mode is silent is worse
 * than a missing one.
 */

interface ShareResult {
  url: string;
  version: number;
  error: string;
}

type Visibility = "private" | "users" | "domain" | "public";

const OPTIONS: { value: Visibility; label: string; reach: string }[] = [
  { value: "private", label: "Only me", reach: "Published, but nobody else can open it" },
  { value: "users", label: "Specific people", reach: "Only the addresses you list" },
  {
    value: "domain",
    label: "Everyone at my company",
    reach: "Anyone whose account email shares yours",
  },
  { value: "public", label: "Anyone with the link", reach: "No sign-in required — treat as published to the world" },
];

/** A stable key for a list of addresses, so the effect above does
 *  not re-run on every render for an array rebuilt each time. */
function listedKey(listed: string[] | undefined): string {
  return (listed ?? []).join("\u0000");
}

/** Where a popover hangs, in viewport coordinates. */
export interface Anchor {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/** Roughly how tall the panel gets, for deciding above or below. */
const PANEL_HEIGHT = 330;
const PANEL_WIDTH = 340;

export function ArtifactShareDialog({
  client,
  artifactId,
  name,
  at,
  reach,
  listed,
  onClose,
}: {
  client: IgniClient;
  artifactId: string;
  name: string;
  /** What it hangs off. Null renders nothing — there is no sensible
   *  place to put a popover with nothing to point at. */
  at: Anchor | null;
  /** How the document is shared today, if it has been. The panel
   *  opens on it rather than on a default — see below. */
  reach?: Visibility;
  listed?: string[];
  onClose: () => void;
}) {
  // Seeded from how the document is shared *now*, not from a default.
  //
  // It opened on "Only me" whatever the document was, which was
  // survivable while this was a page you navigated to and actively
  // wrong the moment it became a popover: it hangs off a line that
  // says "Anyone with the link", and pressing that line to be told
  // "Only me" reads as the panel having lost the setting — or worse,
  // gets published without being looked at, which quietly unshares a
  // document from everybody who had it.
  //
  // A document never shared has no reach and still opens on
  // ``private``, which is the narrowest answer and the right default
  // for a decision this consequential.
  const [visibility, setVisibility] = useState<Visibility>(reach ?? "private");
  // A list, not a line of text. It was a comma-separated string
  // somebody typed from memory, which made "who can read this" a
  // spelling test — and made the audience impossible to *show*,
  // because the control held the only copy of it.
  const [recipients, setRecipients] = useState<string[]>(listed ?? []);
  const [result, setResult] = useState<ShareResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  // Re-seeded when the panel opens on a different document, or when
  // the one it is on has been shared since. Not on every render: the
  // choice being made is the reader's from the moment they open it,
  // and an effect that reset it would fight them as they clicked.
  useEffect(() => {
    setVisibility(reach ?? "private");
    setRecipients(listed ?? []);
    setResult(null);
  }, [artifactId, reach, listedKey(listed)]);

  // Escape closes, from anywhere inside it. Captured on the window
  // rather than on the panel, because the first thing focused here is
  // a radio and a keyboard user who changes their mind should not
  // have to find the Cancel button to say so.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const blocked = visibility === "users" && recipients.length === 0;

  const share = useCallback(async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await client.rpc<ShareResult>("artifact_share", {
        id: artifactId,
        visibility,
        audience: recipients,
      });
      setResult(r);
    } catch (e) {
      setResult({
        url: "",
        version: 0,
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }, [client, artifactId, visibility, recipients]);

  const copy = useCallback(() => {
    if (!result?.url) return;
    void navigator.clipboard?.writeText(result.url).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    });
  }, [result]);

  if (!at) return null;

  // Below when there is room, above when there is not, and pinned to
  // the right edge of whatever opened it — the trigger is near the
  // right of the header, so a panel that grew rightwards would grow
  // off the window.
  const below = window.innerHeight - at.bottom > PANEL_HEIGHT;
  const left = Math.max(12, Math.min(at.right - PANEL_WIDTH, window.innerWidth - PANEL_WIDTH - 12));

  return createPortal(
    <>
      {/* Outside click closes it, the same way the overflow menu's
          backdrop does. */}
      <div className="kb-share-backdrop" onClick={onClose} />
      <div
        ref={panel}
        className="kb-share"
        role="dialog"
        aria-label={`Share ${name}`}
        style={{
          left,
          top: below ? at.bottom + 6 : at.top - 6,
          transform: below ? undefined : "translateY(-100%)",
        }}
      >
      <h3 className="kb-share-title">Share “{name}”</h3>
      <p className="kb-share-sub">
        A snapshot of the current version is published. Later revisions do not
        change what a recipient sees until you share again.
      </p>

      <fieldset className="kb-share-options">
        <legend className="sr-only">Who can open this</legend>
        {OPTIONS.map((option) => (
          <label
            key={option.value}
            className={
              visibility === option.value ? "kb-share-opt kb-share-opt-on" : "kb-share-opt"
            }
          >
            <input
              type="radio"
              name="artifact-visibility"
              value={option.value}
              checked={visibility === option.value}
              onChange={() => setVisibility(option.value)}
            />
            <span className="kb-share-opt-body">
              <span className="kb-share-opt-label">{option.label}</span>
              <span className="kb-share-opt-reach">{option.reach}</span>
            </span>
          </label>
        ))}
      </fieldset>

      {visibility === "users" && (
        <PeoplePicker client={client} chosen={recipients} onChange={setRecipients} />
      )}

      {result?.error && <div className="msg-error">{result.error}</div>}

      {result?.url && (
        <div className="kb-share-link">
          <div className="kb-share-link-label">
            Shared as v{result.version}
          </div>
          <div className="kb-share-link-row">
            <input readOnly value={result.url} onFocus={(e) => e.target.select()} />
            <button className="btn btn-sm" onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      <div className="kb-share-actions">
        <button className="btn btn-sm" onClick={onClose}>
          {result?.url ? "Done" : "Cancel"}
        </button>
        <button
          className="btn btn-sm btn-primary"
          onClick={() => void share()}
          disabled={busy || blocked}
          title={blocked ? "List at least one address" : undefined}
        >
          {busy ? "Publishing…" : result?.url ? "Update" : "Publish"}
        </button>
      </div>
      </div>
    </>,
    document.body,
  );
}
