/**
 * The extension's backend, as a typed object.
 *
 * Everything that knows an RPC name or a wire shape lives here, and
 * nothing else in the extension does. That split is the point of the
 * file: `panel.tsx` renders state and never learns that
 * `ext.acme.scanner.list` exists, so the day a method is renamed or a
 * field changes type, one file changes.
 *
 * ## Why the wire is checked
 *
 * `igni.rpc<T>()` is generic, and a generic is a promise the caller
 * makes to the compiler, not a fact about the bytes. `rpc<Violation[]>`
 * compiles identically whether the backend returns violations, `null`,
 * or an error object — and then the panel throws inside a `.map()`,
 * somewhere far away from the call.
 *
 * It matters more here than in app code. An extension's backend is a
 * separate process on a version the host does not control: a sidecar
 * image pinned at install, or an endpoint somebody else deploys. The
 * two really do drift, and when they do this layer is what turns it
 * into one legible message instead of a blank frame.
 *
 * So every response is parsed. The parsers below are hand-written
 * because this example has three of them and a schema library is a
 * dependency an extension would have to justify; at ten, reach for
 * one. The shape is what matters — a boundary that returns a value
 * or throws a sentence a person can act on.
 */

import type { Bridge } from "@igni/extension-bridge";

/** How bad, worst first. The order is the sort order. */
export const SEVERITIES = ["critical", "high", "medium", "low"] as const;

export type Severity = (typeof SEVERITIES)[number];

export interface Violation {
  id: string;
  rule: string;
  severity: Severity;
  path: string;
  line: number;
}

/**
 * What a scan is doing, as a union rather than flags.
 *
 * It was `{ running: boolean; percent: number | null; step: string }`,
 * which spells four states that cannot happen — not running but at
 * 40%, running with no step — and leaves the one that matters,
 * *failed*, unspellable. A scan that dies is the case the UI most
 * needs to show, and the old shape reported it as "not running",
 * which is also what finishing successfully looks like.
 */
export type ScanStatus =
  | { state: "idle"; lastFinished: string | null }
  | { state: "running"; step: string; percent: number | null }
  | { state: "failed"; message: string };

/** The wire said something this extension cannot read. */
export class WireError extends Error {
  constructor(
    readonly method: string,
    detail: string,
  ) {
    super(`${method} returned something unexpected: ${detail}`);
    this.name = "WireError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  if (typeof value !== "string") {
    throw new TypeError(`${key} should be a string, got ${typeof value}`);
  }
  return value;
}

function parseViolation(raw: unknown, index: number): Violation {
  if (!isRecord(raw)) throw new TypeError(`[${index}] is not an object`);
  const severity = str(raw, "severity");
  if (!(SEVERITIES as readonly string[]).includes(severity)) {
    // Named rather than coerced to "low". A severity the client does
    // not know is usually a backend that has moved on, and quietly
    // filing it under the least urgent bucket is how a critical
    // finding disappears from a compliance report.
    throw new TypeError(
      `[${index}] has severity ${JSON.stringify(severity)}, ` +
        `which this version does not know about`,
    );
  }
  const line = raw.line;
  if (typeof line !== "number" || !Number.isFinite(line)) {
    throw new TypeError(`[${index}] has a line that is not a number`);
  }
  return {
    id: str(raw, "id"),
    rule: str(raw, "rule"),
    severity: severity as Severity,
    path: str(raw, "path"),
    line,
  };
}

function parseStatus(raw: unknown): ScanStatus {
  if (!isRecord(raw)) throw new TypeError("status is not an object");
  switch (raw.state) {
    case "idle": {
      const last = raw.lastFinished;
      return {
        state: "idle",
        lastFinished: typeof last === "string" ? last : null,
      };
    }
    case "running": {
      const percent = raw.percent;
      return {
        state: "running",
        step: str(raw, "step"),
        // A total the backend does not know yet is a legitimate
        // answer, and the bar has an indeterminate mode for it.
        percent:
          typeof percent === "number" && Number.isFinite(percent)
            ? Math.min(100, Math.max(0, percent))
            : null,
      };
    }
    case "failed":
      return { state: "failed", message: str(raw, "message") };
    default:
      throw new TypeError(`state ${JSON.stringify(raw.state)} is not a state`);
  }
}

/**
 * Everything this extension asks of its backend.
 *
 * A class rather than four exported functions, because the bridge is
 * a dependency every call shares: as functions it would be the first
 * argument of each, which is both noisier at the call site and an
 * invitation to reach for a module-level singleton instead.
 */
export class ScannerClient {
  constructor(private readonly igni: Bridge) {}

  /**
   * Call, then check.
   *
   * The parse failure is rethrown as a `WireError` naming the method,
   * so what reaches the UI says which call disagreed rather than
   * `Cannot read properties of undefined`.
   */
  private async call<T>(
    method: string,
    parse: (raw: unknown) => T,
    args?: Record<string, unknown>,
  ): Promise<T> {
    const raw = await this.igni.rpc(method, args);
    try {
      return parse(raw);
    } catch (cause) {
      throw new WireError(
        method,
        cause instanceof Error ? cause.message : String(cause),
      );
    }
  }

  /** Every open violation, worst first. */
  async violations(): Promise<Violation[]> {
    return this.call("ext.acme.scanner.list", (raw) => {
      if (!Array.isArray(raw)) throw new TypeError("not an array");
      const parsed = raw.map(parseViolation);
      parsed.sort(
        (a, b) =>
          SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity) ||
          a.path.localeCompare(b.path) ||
          a.line - b.line,
      );
      return parsed;
    });
  }

  /** What the scan is doing now. */
  async status(): Promise<ScanStatus> {
    return this.call("ext.acme.scanner.status", parseStatus);
  }

  /** Start a scan. Resolves once the backend has accepted it. */
  async startScan(): Promise<void> {
    await this.igni.rpc("ext.acme.scanner.scan");
  }

  /** Accept a violation, with a justification for the audit trail. */
  async accept(id: string, justification: string): Promise<void> {
    await this.igni.rpc("ext.acme.scanner.accept", { id, justification });
  }
}

/**
 * A thrown value as a sentence to put in front of somebody.
 *
 * The host rejects `rpc` with its refusal already worded for a
 * reader — "extension acme.scanner may not call codeindex_status" —
 * so an `Error` is shown as-is. Anything else is a bug in this
 * extension, and `String(value)` is at least honest about it.
 */
export function messageOf(value: unknown): string {
  if (value instanceof Error) return value.message;
  return String(value);
}
