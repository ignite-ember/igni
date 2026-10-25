/**
 * An extension's page, as the app shows it.
 *
 * `ExtensionFrame` is the security boundary — origin, window, nonce
 * and method checks. This is the thing above it that knows about the
 * *app*: which extension this page belongs to, what state it is in,
 * and what to show when that state is not `READY`.
 *
 * Split because the frame was built, tested and never mounted for
 * months, and the reason was that mounting it needs app knowledge the
 * frame deliberately does not have.
 */
import { useCallback, useMemo } from "react";

import { Banner, Button, Empty } from "@igni/ui-react";

import { ExtensionFrame, type ExtensionFrameProps } from "./ExtensionFrame";
import { OpenOnWeb } from "./OpenOnWeb";

/** One extension, as `list_extensions` describes it. */
export interface ExtensionSummary {
  id: string;
  name: string;
  version: string;
  state: string;
  reason: string;
  origin: string;
  allow: string[];
  pages: { kind: string; label: string; sidebar: boolean; entry: string }[];
  menu: { label: string; command: string; desc: string }[];
  /** Status chips this extension puts in the host's own chrome. */
  indicators: {
    id: string;
    label: string;
    source: string;
    opens: string;
    intervalSeconds: number;
  }[];
  /** What it can be configured with, and what it is configured to. */
  settings: {
    key: string;
    type: string;
    label: string;
    default?: unknown;
    options: string[];
  }[];
  values: Record<string, unknown>;
  /**
   * Its home on the web — its service's URL, which is the service's
   * `client_uri` — when one is known. Shown as "Open on the web".
   */
  web_url?: string | null;
  /**
   * Its own service, as its page reaches it (`bridge.service.url`),
   * when it has one a page may reach.
   */
  service_url?: string | null;
  /**
   * What it does not get because it collides with another extension
   * — decided against it on igni server, or not decided yet.
   */
  switched_off?: { claim: string; reason: string }[];
  /**
   * What is wrong with it that does not stop it working — a tool whose
   * server reports other hints than its manifest declares.
   */
  problems?: string[];
  /**
   * The tools it gives the agent, and what each says it is (`read-only,
   * local, idempotent`) — what the permission modes decide on.
   */
  tools?: { name: string; hints: string }[];
}

export interface ExtensionPageProps {
  extension: ExtensionSummary;
  /** Which of its pages to show. */
  kind: string;
  /**
   * Makes the backend call, already bound to this extension.
   *
   * Built with `client.asExtension(id).rpc` by the caller, so every
   * call carries its caller and the backend can enforce the
   * manifest's `permissions.rpc` against it.
   */
  onCall: (method: string, args: Record<string, unknown>) => Promise<unknown>;
  theme?: string;
  /** The frame asked to leave. */
  onClose?: () => void;
  onNavigate?: (to: string) => void;
  /** Host-model signals the backend addressed to this extension. */
  signals?: { signal: string; value: unknown; n: number }[];
  /** Forwards a host-model request (`bridge.host…`) as this extension. */
  onHost?: ExtensionFrameProps["onHost"];
  /** Fetches this extension's service token (`bridge.service`), as it. */
  onServiceToken?: ExtensionFrameProps["onServiceToken"];
  /** Launch parameters — the host's URL state for this extension. */
  params?: Record<string, unknown>;
  onLocation?: (state: Record<string, string>) => void;
}

/**
 * Draw one extension page, or say why it cannot be drawn.
 *
 * Every non-`READY` state gets its own answer rather than a blank
 * frame. `PREPARING` is a wait, `FAILED` and `DISABLED` are a
 * sentence with the reason the backend gave — which the health
 * contract requires to name a fix.
 */
export function ExtensionPage({
  extension,
  kind,
  onCall,
  theme,
  onClose,
  onNavigate,
  signals,
  onHost,
  onServiceToken,
  params,
  onLocation,
}: ExtensionPageProps) {
  const page = useMemo(
    () => extension.pages.find((p) => p.kind === kind),
    [extension.pages, kind],
  );

  // Stable across renders so the frame's listener is not torn down
  // and rebuilt, which drops messages in flight.
  const call = useCallback(onCall, [onCall]);

  if (page === undefined) {
    return (
      <Empty>
        {`'${extension.name}' does not contribute a page called '${kind}'.`}
      </Empty>
    );
  }

  if (extension.state === "PREPARING") {
    return <Banner tone="neutral">{`${extension.name} is starting…`}</Banner>;
  }

  if (extension.state !== "READY") {
    return (
      <Empty
        action={
          onClose ? (
            <Button size="sm" onClick={onClose}>
              Back
            </Button>
          ) : undefined
        }
      >
        {/* The reason, not just the state. "FAILED" on its own sends
            an administrator to the logs; the health contract requires
            a reason and a fix, and this is where they are read. */}
        {extension.reason
          ? `${extension.name} is ${extension.state}. ${extension.reason}`
          : `${extension.name} is ${extension.state}.`}
      </Empty>
    );
  }

  if (!extension.origin) {
    // READY with pages and no origin should be impossible — the
    // installer marks that FAILED — so this is a guard against a
    // backend that disagrees with itself, not an expected state.
    return (
      <Empty>
        {`${extension.name} is READY but its UI is not being served.`}
      </Empty>
    );
  }

  const frame = (
    <ExtensionFrame
      extensionId={extension.id}
      origin={extension.origin}
      path={`/${page.entry}`}
      allow={extension.allow}
      onCall={call}
      onClose={onClose}
      onNavigate={onNavigate}
      onLocation={onLocation}
      signals={signals}
      onHost={onHost}
      onServiceToken={onServiceToken}
      serviceUrl={extension.service_url ?? null}
      params={params}
      // What an administrator set it to, from `list_extensions`.
      // The backend has already filled defaults in and dropped keys
      // the manifest no longer declares, so this is exactly the
      // shape `contributes.settings` describes.
      settings={extension.values}
      theme={theme}
      title={page.label}
    />
  );
  if (!extension.web_url) return frame;
  return (
    <div className="extension-page">
      <div className="extension-page-bar">
        <OpenOnWeb url={extension.web_url} name={extension.name} />
      </div>
      {frame}
    </div>
  );
}
