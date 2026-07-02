import { useState } from "react";

/** Mode badge — small chip rendered in the status line when
 *  ``permission_mode`` is any non-default value. Different
 *  styling per mode so the user can tell at a glance whether
 *  the sandbox is tightened (plan, dontAsk) or loosened
 *  (acceptEdits, bypassPermissions). Hidden in ``default`` mode
 *  so the badge has signal value (you only see it when it
 *  matters).
 *
 *  CC parity:
 *  * row 50 — ``plan`` (read-only sandbox)
 *  * row 51 — ``acceptEdits`` (auto-approve edits)
 *  * row 7  — ``dontAsk`` / ``bypassPermissions`` future modes
 *
 *  ``PlanBadge`` is kept as a legacy alias — call sites that only
 *  care about plan mode can use it. New surfaces should use
 *  ``ModeBadge``. */
type ModeBadgeStyle = {
  className: string;
  label: string;
  title: string;
};

const MODE_BADGE_STYLES: Record<string, ModeBadgeStyle> = {
  plan: {
    className: "plan-badge",
    label: "PLAN MODE",
    title:
      "In plan mode — the agent can read + search but cannot edit files or run mutating commands. Type /plan again to exit.",
  },
  acceptEdits: {
    className: "plan-badge mode-badge--accept",
    label: "ACCEPT EDITS",
    title:
      "In acceptEdits mode — file-edit tools auto-approve. Use /accept off to leave.",
  },
  dontAsk: {
    className: "plan-badge mode-badge--dontask",
    label: "STRICT (DONT-ASK)",
    title:
      "In dontAsk mode — anything without an explicit allow rule is denied. Strictest non-plan mode.",
  },
  bypassPermissions: {
    className: "plan-badge mode-badge--bypass",
    label: "BYPASS PERMISSIONS",
    title:
      "In bypassPermissions mode — most permission gates skipped. Scoped denies still hold (e.g. .env protection).",
  },
};

export function ModeBadge({ mode }: { mode: string }) {
  const style = MODE_BADGE_STYLES[mode];
  if (!style) return null;
  return (
    <span className={style.className} title={style.title}>
      <span className="plan-badge-dot" /> {style.label}
    </span>
  );
}

/** Legacy alias — same behaviour as the previous plan-only
 *  badge. Existing call sites kept working without touching them. */
export const PlanBadge = ModeBadge;

/** Footer "Auto-approve" switch — one-click toggle into
 *  ``bypassPermissions`` mode. When ON, the agent runs any tool
 *  without asking (HITL prompts skipped); when OFF, the default
 *  per-tool ask policy applies. Scoped denies (e.g. ``.env``)
 *  still block in either state. The switch is intentionally
 *  loud when active — red pill + dot — so the user can't forget
 *  it's on. Session-only: every new session starts OFF. */
export function AutoApproveSwitch({
  mode,
  onToggle,
}: {
  mode: string;
  onToggle: (next: boolean) => void;
}) {
  const active = mode === "bypassPermissions";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={active}
      className={`auto-approve-switch${active ? " is-on" : ""}`}
      title={
        active
          ? "Auto-approve is ON — every tool runs without asking. Click to turn off."
          : "Auto-approve is OFF — tools ask for permission. Click to let the agent continue without prompts."
      }
      onClick={() => onToggle(!active)}
    >
      <span className="auto-approve-track">
        <span className="auto-approve-thumb" />
      </span>
      <span className="auto-approve-label">
        Auto-approve{active ? " ON" : ""}
      </span>
    </button>
  );
}

const fmtTokens = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);

/** Click to copy the full session id. Visible label is the short
 *  prefix the BE uses everywhere else. */
export function SessionChip({ sessionId }: { sessionId: string }) {
  const [copied, setCopied] = useState(false);

  if (!sessionId) return <span>session —</span>;

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(sessionId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard API blocked (insecure context, etc.); silently ignore.
    }
  };

  // First 8 hex chars only — what the BE uses as the canonical
  // session id today. Older sessions persisted with a longer id
  // (when ``persistence.fork`` was using ``uuid.uuid4().hex``)
  // get the same treatment; click-to-copy still ships the full
  // string so consumers that need the original keep working.
  const short = sessionId.slice(0, 8);

  return (
    <button
      type="button"
      className={`session-chip${copied ? " copied" : ""}`}
      title={copied ? "Copied!" : `Copy ${sessionId}`}
      onClick={onCopy}
    >
      <span className="session-chip-label">session</span>
      <code>{short}</code>
      {copied && <span className="session-chip-toast">copied</span>}
    </button>
  );
}

/** Backend-version chip for the JetBrains plugin — renders only when
 *  the plugin injects ``expected_cli`` + ``actual_cli`` + ``backend_source``
 *  query params into the JCEF page URL. In every other host (Tauri,
 *  VSCode webview, plain browser) the params are absent and this
 *  component paints nothing.
 *
 *  Three visual states:
 *
 *   * ``ok``       — actual == expected AND managed venv is active.
 *                    Small neutral chip: "cli · vX.Y.Z".
 *   * ``dev``      — running through EMBER_DEV_BACKEND override.
 *                    Amber chip; hover text names the env var.
 *   * ``mismatch`` — actual != expected. Red chip; tooltip suggests
 *                    Tools → igni → Diagnose Backend.
 *
 *  The chip is a passive display — it doesn't need RPC roundtripping
 *  or dependency on the running BE, because the plugin embeds the
 *  values it *already* resolved at bootstrap. If we can't render this
 *  because the BE is down, that's exactly when the chip is useful:
 *  a bootstrap failure often leaves the plugin sitting on a stale
 *  interpreter, and the chip is the fastest way for the reader to
 *  see it.
 */
export function BackendVersionChip() {
  const info = readBackendVersionParams();
  if (!info) return null;

  const mismatch =
    info.actual && info.expected && info.actual !== info.expected;
  const dev = info.source === "dev_override";
  const tone = mismatch ? "danger" : dev ? "warn" : "ok";

  const label =
    info.actual === "unknown"
      ? "cli · <probe failed>"
      : `cli · v${info.actual}`;

  const title = mismatch
    ? `Running ignite-ember v${info.actual}, expected v${info.expected}. ` +
      "Tools → igni → Diagnose Backend for details."
    : dev
      ? `EMBER_DEV_BACKEND override active — running the interpreter set in your ` +
        `environment (v${info.actual}). Managed venv is bypassed.`
      : `ignite-ember v${info.actual}, managed venv.`;

  return (
    <span className={`backend-version-chip tone-${tone}`} title={title}>
      {label}
    </span>
  );
}

interface BackendVersionInfo {
  expected: string;
  actual: string;
  source: string;
}

/** Parse the version-info the host embedded on the current page.
 *
 *  Two delivery mechanisms, checked in order:
 *
 *   1. URL query params (``?expected_cli=…&actual_cli=…&backend_source=…``)
 *      — used by the JetBrains JCEF panel and the Tauri WKWebView,
 *      both of which control the top-level URL directly.
 *   2. ``<meta>`` tags of the same name — used by the VSCode
 *      webview, which delivers HTML wholesale and can't rely on
 *      query params under the default CSP.
 *
 *  Kept as a plain function (not a hook) so it runs once at mount
 *  — the values don't change over the tool-window / panel lifetime.
 */
function readBackendVersionParams(): BackendVersionInfo | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const meta = (name: string): string | null => {
    if (typeof document === "undefined") return null;
    const el = document.querySelector(
      `meta[name="${name}"]`,
    ) as HTMLMetaElement | null;
    return el?.content?.trim() || null;
  };
  const expected = params.get("expected_cli") ?? meta("ember-expected-cli");
  const actual = params.get("actual_cli") ?? meta("ember-actual-cli");
  const source = params.get("backend_source") ?? meta("ember-backend-source");
  // All three must be present. Absence = "no host embedded them,
  // don't render the chip".
  if (!expected || !actual || !source) return null;
  return { expected, actual, source };
}

/** Context meter: a slim bar that fills as the conversation grows,
 *  color-graded (calm → warning → danger). The numeric readout sits
 *  next to it so the user can read both at a glance. */
export function CtxMeter({
  tokens,
  max,
  pct,
}: {
  tokens: number;
  max: number;
  pct: number;
}) {
  const safe = Math.min(100, Math.max(0, pct));
  const tone = safe >= 85 ? "danger" : safe >= 60 ? "warn" : "ok";
  return (
    <span
      className={`ctx-meter tone-${tone}`}
      title={
        max
          ? `${tokens.toLocaleString()} of ${max.toLocaleString()} tokens used`
          : `${tokens.toLocaleString()} tokens`
      }
    >
      <span className="ctx-meter-label">ctx</span>
      <span className="ctx-meter-track">
        <span className="ctx-meter-fill" style={{ width: `${safe}%` }} />
      </span>
      <span className="ctx-meter-text">
        {fmtTokens(tokens)} <span className="ctx-meter-pct">· {safe}%</span>
      </span>
    </span>
  );
}
