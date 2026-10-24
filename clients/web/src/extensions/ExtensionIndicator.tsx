/**
 * An extension's status chip, in igni's own chrome.
 *
 * `CodeIndexIndicator` sits in the composer today, hardcoded, beside
 * `WatcherIndicator` and `GroupIndicator`. When CodeIndex becomes an
 * extension that chip has to come from the manifest or be lost —
 * and `indicators` had been moved to "not in v1" on the grounds that
 * nothing specified it, which was right about the specification and
 * wrong about the need.
 *
 * **Rendered by the host, decided by the extension.** A chip is
 * twenty pixels and an iframe is not what twenty pixels costs, so
 * this is data rather than a frame. The extension answers with
 * `{ label, tone, detail }` and the host draws it — which also puts
 * the judgement in the right place, because what "preparing" means
 * for an index is not what it means for a document store, and only
 * the capability knows.
 *
 * **Polled, because the bridge has no push.** The interval is the
 * manifest's, floored by the host: an extension that asked to be
 * polled ten times a second would be asking for igni's main thread.
 */
import { useEffect, useRef, useState } from "react";

import { Status, Tooltip } from "@igni/ui-react";

import { badgeText } from "../notifications/model";

/** The four the design system publishes, and nothing else. */
const TONES = ["neutral", "success", "warning", "danger"] as const;

type IndicatorTone = (typeof TONES)[number];

/** What the chip shows. The extension's `source` tool answers this. */
export interface IndicatorState {
  label: string;
  tone: IndicatorTone;
  detail: string;
}

/** The four states a `Status` renders, mapped from a tone. */
const STATE_FOR: Record<IndicatorTone, "READY" | "PREPARING" | "FAILED" | "DISABLED"> = {
  success: "READY",
  warning: "PREPARING",
  danger: "FAILED",
  neutral: "DISABLED",
};

/** Nothing faster than this, whatever a manifest asks for. */
const FLOOR_SECONDS = 5;

export interface ExtensionIndicatorProps {
  extensionId: string;
  /** The indicator as the manifest declared it. */
  indicator: {
    id: string;
    label: string;
    source: string;
    opens: string;
    intervalSeconds: number;
  };
  /** Bound to this extension, so the backend can authorise it. */
  onCall: (method: string, args: Record<string, unknown>) => Promise<unknown>;
  /** The user clicked it. Absent when the manifest named no page. */
  onOpen?: (pageKind: string) => void;
  /**
   * Unread notifications from this extension — the floor's count, not
   * the extension's. igni server's and this machine's together; the
   * extension does not compute it and cannot change it.
   */
  unread?: number;
}

/** Is this what an indicator source is supposed to answer with? */
export function isIndicatorState(value: unknown): value is IndicatorState {
  if (typeof value !== "object" || value === null) return false;
  const state = value as Record<string, unknown>;
  return (
    typeof state.label === "string" &&
    typeof state.tone === "string" &&
    (TONES as readonly string[]).includes(state.tone)
  );
}

export function ExtensionIndicator({
  extensionId,
  indicator,
  onCall,
  onOpen,
  unread = 0,
}: ExtensionIndicatorProps) {
  const [state, setState] = useState<IndicatorState | null>(null);
  const call = useRef(onCall);
  call.current = onCall;

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const every = Math.max(FLOOR_SECONDS, indicator.intervalSeconds) * 1000;

    const tick = async () => {
      try {
        const answer = await call.current(
          `ext.${extensionId}.${indicator.source}`,
          {},
        );
        // Checked rather than cast. A chip that renders whatever
        // arrived would put an extension's typo into igni's chrome,
        // and `tone` picks a colour — so a value outside the four is
        // a chip in a colour igni does not have.
        if (alive) setState(isIndicatorState(answer) ? answer : null);
      } catch {
        // A refused or failed call is not worth a chip of its own:
        // the extension's own state already shows FAILED in the
        // list, with a reason. Silence here, rather than two places
        // saying the same thing differently.
        if (alive) setState(null);
      }
      // Self-scheduling, so a slow answer cannot queue up behind
      // itself the way `setInterval` lets it.
      if (alive) timer = setTimeout(() => void tick(), every);
    };

    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [extensionId, indicator.source, indicator.intervalSeconds]);

  const badge =
    unread > 0 ? (
      <span className="extension-badge" aria-label={`${unread} unread`}>
        {badgeText(unread)}
      </span>
    ) : null;

  // Nothing to say yet, or the source answered with something that
  // is not an indicator state. An absent chip is better than a
  // wrong one — unless something is unread: that count is the
  // floor's, and it does not wait for the extension to answer.
  if (state === null) {
    if (badge === null) return null;
    const label = (
      <span className="extension-indicator-unread">
        {indicator.label}
        {badge}
      </span>
    );
    if (!indicator.opens || onOpen === undefined) return label;
    return (
      <button
        type="button"
        className="btn btn-quiet btn-xs"
        aria-label={`${indicator.label}: ${unread} unread. Open.`}
        onClick={() => onOpen(indicator.opens)}
      >
        {label}
      </button>
    );
  }

  const chip = (
    <Status state={STATE_FOR[state.tone]}>
      {state.label || indicator.label}
      {badge}
    </Status>
  );

  if (!indicator.opens || onOpen === undefined) {
    return state.detail ? <Tooltip text={state.detail}>{chip}</Tooltip> : chip;
  }

  const button = (
    <button
      type="button"
      className="btn btn-quiet btn-xs"
      // The label is the state; the name says what clicking does.
      aria-label={`${indicator.label}: ${state.label}${unread > 0 ? `, ${unread} unread` : ""}. Open.`}
      onClick={() => onOpen(indicator.opens)}
    >
      {chip}
    </button>
  );

  return state.detail ? <Tooltip text={state.detail}>{button}</Tooltip> : button;
}
