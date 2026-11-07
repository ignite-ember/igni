import { useState } from "react";
import type { IgniClient } from "../../protocol/client";
import { Drawer } from "./Drawer";

/**
 * Programs — who besides this window may answer the agent's approval
 * requests.
 *
 * A program attached to this igni (the SDK, a script) runs sessions but
 * approves nothing: the token it attaches with sits in the project's
 * lockfile, where the agent can read it too. To let one program approve,
 * the person makes an approver token here and gives it to that program.
 * The backend keeps it in memory only, so quitting igni forgets it, and
 * this window is the only place one is made (``SECURITY.md`` §10).
 */
export function ProgramsPanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [token, setToken] = useState("");
  const [revoked, setRevoked] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const make = () =>
    run(async () => {
      const result = await client.rpc<{ token: string }>("mint_approver_token");
      setToken(result.token);
      setRevoked(null);
      setCopied(false);
    });

  const revokeAll = () =>
    run(async () => {
      const result = await client.rpc<{ revoked: number }>("revoke_approver_tokens");
      setToken("");
      setRevoked(result.revoked);
    });

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard API blocked (insecure context, etc.); the token is
      // still on screen to select.
    }
  };

  return (
    <Drawer title="Programs" onClose={onClose}>
      <div className="programs">
        <p className="programs-lead">
          A program attached to igni, such as a script using the SDK, can run
          sessions but cannot answer the agent's approval requests. To let one
          approve, make an approver token and give it to that program.
        </p>
        <p className="programs-note">
          igni keeps approver tokens in memory only: they stop working when you
          quit igni or revoke them. A program can never switch approvals off,
          change permission rules, or install anything, even with a token.
        </p>
        {error && <div className="msg-error">{error}</div>}
        {token ? (
          <div className="programs-token">
            <span className="programs-token-label">
              Approver token. It is shown once, so copy it now.
            </span>
            <code className="programs-token-value">{token}</code>
            <div className="programs-actions">
              <button className="btn btn-sm" onClick={() => void copy()}>
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        ) : null}
        {revoked !== null && (
          <p className="programs-note">
            {revoked === 0
              ? "There were no approver tokens to revoke."
              : `Revoked ${revoked} approver ${revoked === 1 ? "token" : "tokens"}. Programs already connected keep approving until they disconnect.`}
          </p>
        )}
        <div className="programs-actions">
          <button className="btn btn-sm" disabled={busy} onClick={() => void make()}>
            Make an approver token
          </button>
          <button
            className="btn btn-sm btn-danger"
            disabled={busy}
            onClick={() => void revokeAll()}
          >
            Revoke all approver tokens
          </button>
        </div>
      </div>
    </Drawer>
  );
}
