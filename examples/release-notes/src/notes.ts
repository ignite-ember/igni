/**
 * The Release Notes backend, as a typed object.
 *
 * Same split as the reference extension: everything that knows an RPC
 * name or a wire shape is here, and `panel.tsx` renders state. The
 * difference is what this one has to carry — a document is editable,
 * so the client has a save with a concurrency token rather than a
 * read and a scan.
 */

import type { Bridge } from "@igni/extension-bridge";

/** Where a release is in its life. */
export const STATES = ["draft", "published", "superseded"] as const;

export type ReleaseState = (typeof STATES)[number];

export interface Release {
  version: string;
  state: ReleaseState;
  /** ISO 8601, or null for a draft that has never been published. */
  publishedAt: string | null;
}

export interface Note {
  version: string;
  /** Markdown. Block structure matters, which is the point. */
  body: string;
  /**
   * What the server had when it sent this.
   *
   * Sent back on save so the server can refuse a write over somebody
   * else's. A document is the one shape where two people editing is
   * normal rather than exceptional, and "last write wins" there means
   * somebody's afternoon is gone with no error.
   */
  revision: string;
}

export interface Comment {
  id: string;
  version: string;
  author: string;
  body: string;
  resolved: boolean;
}

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

/** Somebody else saved first. */
export class ConflictError extends Error {
  constructor(readonly theirRevision: string) {
    super(
      "Somebody else saved this note while you were editing it. " +
        "Your text is still here — copy what you need before reloading.",
    );
    this.name = "ConflictError";
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

function parseRelease(raw: unknown, index: number): Release {
  if (!isRecord(raw)) throw new TypeError(`[${index}] is not an object`);
  const state = str(raw, "state");
  if (!(STATES as readonly string[]).includes(state)) {
    throw new TypeError(
      `[${index}] has state ${JSON.stringify(state)}, which this version ` +
        "does not know about",
    );
  }
  const published = raw.publishedAt;
  return {
    version: str(raw, "version"),
    state: state as ReleaseState,
    publishedAt: typeof published === "string" ? published : null,
  };
}

function parseNote(raw: unknown): Note {
  if (!isRecord(raw)) throw new TypeError("note is not an object");
  return {
    version: str(raw, "version"),
    body: str(raw, "body"),
    revision: str(raw, "revision"),
  };
}

function parseComment(raw: unknown, index: number): Comment {
  if (!isRecord(raw)) throw new TypeError(`[${index}] is not an object`);
  return {
    id: str(raw, "id"),
    version: str(raw, "version"),
    author: str(raw, "author"),
    body: str(raw, "body"),
    resolved: raw.resolved === true,
  };
}

/** Everything this extension asks of its backend. */
export class NotesClient {
  constructor(private readonly igni: Bridge) {}

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

  /** Every release, newest first. */
  async releases(): Promise<Release[]> {
    return this.call("ext.acme.notes.releases", (raw) => {
      if (!Array.isArray(raw)) throw new TypeError("not an array");
      return raw.map(parseRelease);
    });
  }

  /** One release's note. */
  async note(version: string): Promise<Note> {
    return this.call("ext.acme.notes.note", parseNote, { version });
  }

  /** Comments on one release. */
  async comments(version: string): Promise<Comment[]> {
    return this.call(
      "ext.acme.notes.comments",
      (raw) => {
        if (!Array.isArray(raw)) throw new TypeError("not an array");
        return raw.map(parseComment);
      },
      { version },
    );
  }

  /**
   * Save a draft, refusing to overwrite somebody else's.
   *
   * The server answers `{ conflict: "<their revision>" }` rather than
   * failing, because a conflict is an outcome and not a fault — the
   * UI has to keep the text either way, and an exception path that
   * also carries data is a worse shape than a union.
   */
  async save(version: string, body: string, revision: string): Promise<Note> {
    const raw = await this.igni.rpc("ext.acme.notes.save", {
      version,
      body,
      revision,
    });
    if (isRecord(raw) && typeof raw.conflict === "string") {
      throw new ConflictError(raw.conflict);
    }
    try {
      return parseNote(raw);
    } catch (cause) {
      throw new WireError(
        "ext.acme.notes.save",
        cause instanceof Error ? cause.message : String(cause),
      );
    }
  }

  /** Publish a draft. */
  async publish(version: string): Promise<void> {
    await this.igni.rpc("ext.acme.notes.publish", { version });
  }
}

/** A thrown value as a sentence to put in front of somebody. */
export function messageOf(value: unknown): string {
  if (value instanceof Error) return value.message;
  return String(value);
}
