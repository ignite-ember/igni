/**
 * What a session without an account sees, which is this and nothing else.
 *
 * igni is self-hosted and every document, every version and every
 * comment lives on the server an organisation runs. A session with no
 * account has nowhere to put any of it — so the app does not open
 * behind a dismissable prompt, it opens here.
 *
 * **It asks for an email, not a URL.** "Paste your igni server
 * address" asks for something most people do not have; the domain in
 * their address is already the name of the organisation whose igni
 * they should reach, and that organisation publishes where it is —
 * in its own zone, on its own web root. Nothing vendor-run is asked,
 * which is why the field can be an address at all.
 *
 * Most domains publish nothing, and that is not a failure. The
 * address field becomes a server field, which is the honest fallback
 * and the only thing that works for an intranet host or a bare IP.
 * The reason discovery gave is shown there rather than swallowed —
 * "acme.example has no igni record published" tells an admin what to
 * go and do.
 *
 * Signing in itself is not reimplemented here. It hands off to the
 * deployment's own sign-in page, which already renders that
 * deployment's methods: password, whichever providers it enabled, and
 * its SSO button under the name its administrator chose.
 */
import { useState } from "react";

import type { IgniClient } from "../protocol/client";

type Found = {
  api_url?: string;
  portal_url?: string;
  source?: string;
  reason?: string;
  error?: string;
};

/** Which question the wall is asking. */
type Step = "email" | "server" | "ready";

export function SignInWall({
  client,
  onSignIn,
}: {
  client: IgniClient;
  /** Start the browser sign-in against the server now configured. */
  onSignIn: () => void;
}) {
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [server, setServer] = useState("");
  const [found, setFound] = useState<Found | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");

  const look = async () => {
    if (!email.trim() || busy) return;
    setBusy(true);
    setFailed("");
    try {
      const answer = await client.rpc<Found>("discover_server", {
        email: email.trim(),
      });
      if (answer?.api_url) {
        // Found. Write it down before offering to sign in — the
        // sign-in flow reads the configured server, so an unsaved
        // one would send them to whatever was there before.
        const saved = await client.rpc<Found>("set_server", {
          api_url: answer.api_url,
          portal_url: answer.portal_url ?? "",
        });
        if (saved?.error) {
          setFailed(saved.error);
          return;
        }
        setFound(answer);
        setStep("ready");
        return;
      }
      // Nothing published. Ask, and carry the reason across so the
      // field explains itself rather than appearing out of nowhere.
      setFound(answer ?? null);
      setStep("server");
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!server.trim() || busy) return;
    setBusy(true);
    setFailed("");
    try {
      const saved = await client.rpc<Found>("set_server", {
        api_url: server.trim(),
      });
      if (saved?.error) {
        setFailed(saved.error);
        return;
      }
      setFound({ api_url: saved?.api_url ?? server.trim() });
      setStep("ready");
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const host = (url: string) => {
    try {
      return new URL(url).host;
    } catch {
      return url;
    }
  };

  return (
    <div className="wall">
      <div className="wall-card">
        <div className="wall-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32" width="30" height="30" fill="none">
            <path
              d="M16 3c3 5 7 6.5 7 12a7 7 0 0 1-14 0c0-5.5 4-7 7-12z"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinejoin="round"
            />
          </svg>
        </div>

        {step === "email" && (
          <>
            <h1 className="wall-title">Sign in to igni</h1>
            <p className="wall-lead">
              Your work lives on your organisation's igni. Tell us your address
              and we'll find it.
            </p>
            <form
              className="wall-form"
              onSubmit={(e) => {
                e.preventDefault();
                void look();
              }}
            >
              <input
                className="wall-input"
                type="email"
                autoFocus
                aria-label="Work email"
                placeholder="you@yourcompany.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
              <button
                className="btn btn-primary wall-go"
                type="submit"
                disabled={busy || !email.trim()}
              >
                {busy ? "Looking…" : "Continue"}
              </button>
            </form>
          </>
        )}

        {step === "server" && (
          <>
            <h1 className="wall-title">Where is your igni?</h1>
            {/* The reason, not a shrug. An administrator reading
                "acme.example has no igni record published" knows what
                to go and publish. */}
            <p className="wall-lead">
              {found?.reason || "We could not find it from your address."} Ask
              whoever runs it, and paste the address here.
            </p>
            <form
              className="wall-form"
              onSubmit={(e) => {
                e.preventDefault();
                void save();
              }}
            >
              <input
                className="wall-input"
                type="url"
                autoFocus
                aria-label="igni server address"
                placeholder="https://igni.yourcompany.com"
                value={server}
                onChange={(e) => setServer(e.target.value)}
              />
              <button
                className="btn btn-primary wall-go"
                type="submit"
                disabled={busy || !server.trim()}
              >
                {busy ? "Saving…" : "Connect"}
              </button>
            </form>
            <button
              className="wall-back"
              type="button"
              onClick={() => {
                setFailed("");
                setStep("email");
              }}
            >
              Use a different address
            </button>
          </>
        )}

        {step === "ready" && (
          <>
            <h1 className="wall-title">Sign in to igni</h1>
            <p className="wall-lead">
              {/* Named, so nobody signs in to the wrong company's
                  deployment without noticing. */}
              Connecting to <strong>{host(found?.api_url ?? "")}</strong>
              {found?.source === "well-known" || found?.source === "dns"
                ? ", found from your address."
                : "."}
            </p>
            <button
              className="btn btn-primary wall-go"
              type="button"
              autoFocus
              onClick={onSignIn}
            >
              Sign in
            </button>
            <button
              className="wall-back"
              type="button"
              onClick={() => {
                setFailed("");
                setServer("");
                setStep("server");
              }}
            >
              Use a different server
            </button>
          </>
        )}

        {failed && <p className="wall-error">{failed}</p>}

        <p className="wall-foot">
          igni is self-hosted. Your documents stay on your organisation's
          server.
        </p>
      </div>
    </div>
  );
}
