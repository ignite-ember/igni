import { useCallback, useMemo, useState } from "react";
import type { IgniClient } from "../../protocol/client";

/**
 * Choose who can open a document, and publish it.
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

export function ArtifactShareDialog({
  client,
  artifactId,
  name,
  onClose,
}: {
  client: IgniClient;
  artifactId: string;
  name: string;
  onClose: () => void;
}) {
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [audience, setAudience] = useState("");
  const [result, setResult] = useState<ShareResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const recipients = useMemo(
    () =>
      audience
        .split(/[\s,;]+/)
        .map((s) => s.trim())
        .filter(Boolean),
    [audience],
  );

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

  return (
    <div className="kb-share">
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
        <div className="kb-share-audience">
          <label htmlFor="kb-audience">Email addresses</label>
          <textarea
            id="kb-audience"
            rows={2}
            value={audience}
            placeholder="ana@acme.co, sam@acme.co"
            onChange={(e) => setAudience(e.target.value)}
          />
          <span className="kb-hint">
            {recipients.length
              ? `${recipients.length} recipient${recipients.length === 1 ? "" : "s"}`
              : "Separate with commas or spaces"}
          </span>
        </div>
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
  );
}
