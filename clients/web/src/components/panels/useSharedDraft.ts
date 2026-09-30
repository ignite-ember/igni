/**
 * The draft, as a thing two people can type into at once.
 *
 * A document open in two windows used to be two documents. Each one
 * wrote the whole draft to the server every time the typing paused,
 * so the last window to pause won and the other found out on
 * refresh — which is not collaboration, it is a race with a slow
 * scoreboard.
 *
 * So the draft is a CRDT. Every keystroke is a small opaque change;
 * changes commute, so applying them in any order lands both windows
 * on the same document, and nobody has to merge anything. That last
 * part is not an implementation detail — **the server never merges**.
 * It relays the changes, keeps them in a list, and has no opinion
 * about what they say. The only thing in this system that can turn a
 * pile of changes into a document is an editor, and the editors are
 * here.
 *
 * **The socket is not here either.** It belongs to this machine's
 * backend, where the cloud credentials live, exactly as presence
 * does — see ``useDocumentRoom``. This hook talks to the backend and
 * the backend talks to the room.
 *
 * **Seeding.** A document being edited for the first time has an
 * empty change list and has to get its text from somewhere: the
 * published version, or a draft written before any of this existed.
 * Two windows opening at the same moment would both seed it, and the
 * document would say everything twice — so the seed is written under
 * a fixed client id, which makes the two seeds the *same* change
 * rather than two of them. Applying it twice is applying it once.
 *
 * The seeding itself is the editor's, through :func:`seedOnce`. It
 * holds the markdown parser and the schema, and a second parser here
 * would be a second opinion about what a nested list is — identical
 * until the day it was not, at which point the two windows would
 * seed differently and the trick above would produce two documents
 * instead of one.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import * as Y from "yjs";

import type { IgniClient } from "../../protocol/client";

/** The id every seeding transaction is written under. See above. */
const SEED_CLIENT_ID = 1;

/** The Yjs field the editor binds to. Must match the extension's. */
export const DRAFT_FIELD = "default";

interface DraftPush {
  channel?: string;
  payload?: { artifact_id?: string; frame?: RoomFrame };
}

interface RoomFrame {
  type?: string;
  /** One change, base64. */
  update?: string;
  /** Every change so far, base64, oldest first. */
  updates?: string[];
  base_n?: number;
}

function toBytes(base64: string): Uint8Array {
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function toBase64(bytes: Uint8Array): string {
  let raw = "";
  // In chunks: ``String.fromCharCode(...bytes)`` on a whole document
  // is an argument per byte, and a long one overflows the stack.
  for (let i = 0; i < bytes.length; i += 0x8000) {
    raw += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(raw);
}

/**
 * Whether this document has any content yet.
 *
 * Asked of the Yjs fragment rather than of the change list: a list
 * can be non-empty and say nothing — a window that joined, typed
 * nothing and left still wrote presence-shaped noise — and what
 * matters before seeding is whether there are any words.
 */
export function isEmpty(doc: Y.Doc): boolean {
  return doc.getXmlFragment(DRAFT_FIELD).length === 0;
}

export interface SharedDraft {
  /** The document the editor binds to, or nothing while it loads. */
  doc: Y.Doc | null;
  /** True once the room's changes have been applied — before that,
   *  the document is empty and must not be shown as though it were
   *  the draft. */
  ready: boolean;
  /** Whether this is actually shared. A document nobody has shared
   *  has no room, so there is nobody to be live with. */
  live: boolean;
}

export function useSharedDraft(
  client: IgniClient,
  artifactId: string,
  /** Only while the editor is open. A reader does not need a live
   *  copy of somebody else's half-finished sentence under the
   *  version they are reading. */
  active: boolean,
): SharedDraft {
  const [ready, setReady] = useState(false);
  const [live, setLive] = useState(false);
  // One per document, and rebuilt when the document changes. A Y.Doc
  // carries its whole history; reusing one across documents would
  // put the last document's sentences in this one.
  const doc = useMemo(() => (active && artifactId ? new Y.Doc() : null), [active, artifactId]);
  const seeded = useRef(false);

  useEffect(() => {
    if (!doc) return;
    return () => doc.destroy();
  }, [doc]);

  // Everything arriving from the room.
  useEffect(() => {
    if (!doc || !artifactId) return;
    setReady(false);
    seeded.current = false;

    // Settled means "the room has told us what it has". Whether the
    // document needs seeding is then a question about the document,
    // asked by whoever can answer it — the editor.
    const settle = () => {
      if (seeded.current) return;
      seeded.current = true;
      setReady(true);
    };

    const off = client.onEvent((message) => {
      const push = message as unknown as DraftPush;
      if (push.channel !== "artifact_draft") return;
      if (push.payload?.artifact_id !== artifactId) return;
      const frame = push.payload?.frame ?? {};

      if (frame.type === "draft") {
        for (const update of frame.updates ?? []) {
          // ``"room"`` marks changes that came from somebody else, so
          // the observer below knows not to send them back.
          Y.applyUpdate(doc, toBytes(update), "room");
        }
        setLive(true);
        settle();
      } else if (frame.type === "edit" && frame.update) {
        Y.applyUpdate(doc, toBytes(frame.update), "room");
      } else if (frame.type === "snapshot") {
        // The server asking for the whole document, because its list
        // of changes has grown long. It cannot build one itself —
        // that is the point — so it asks whoever is typing.
        void client
          .rpc("artifact_room_send", {
            artifact_id: artifactId,
            frame: { type: "snapshot", update: toBase64(Y.encodeStateAsUpdate(doc)) },
          })
          .catch(() => {});
      }
    });

    // A backstop, not the usual path. Every document answers with a
    // draft frame — a shared one from the server, a local one from
    // this machine's own log — so this only fires when the backend
    // cannot be reached at all, and then it settles empty so the
    // editor opens on the text rather than on nothing.
    const alone = window.setTimeout(settle, 2000);

    return () => {
      off();
      window.clearTimeout(alone);
    };
  }, [client, doc, artifactId]);

  // Everything going the other way.
  useEffect(() => {
    if (!doc || !artifactId) return;
    const onUpdate = (update: Uint8Array, origin: unknown) => {
      // Not what we just received. Sending it back would loop it
      // round the room for ever, and it changes nothing on the way.
      if (origin === "room") return;
      void client
        .rpc("artifact_room_send", {
          artifact_id: artifactId,
          frame: { type: "edit", update: toBase64(update) },
        })
        .catch(() => {
          // The room is not open, or the backend cannot reach it.
          // Dropped rather than queued: the draft is still written
          // over HTTP when the typing pauses, so nothing is lost —
          // the other window simply finds out a second later.
        });
    };
    doc.on("update", onUpdate);
    return () => doc.off("update", onUpdate);
  }, [client, doc, artifactId]);

  return { doc, ready, live };
}

/**
 * Write the starting content, if nobody has written any.
 *
 * ``write`` is called inside a transaction stamped with the fixed
 * seeding id, so two windows doing this at the same moment produce
 * the same change rather than two copies of the document. It must
 * therefore write the *same thing* on both: the same text, parsed
 * the same way, which is why it is the editor that passes it.
 *
 * Answers whether it wrote anything, so the caller can tell "I put
 * the text there" from "somebody had already started".
 */
export function seedOnce(doc: Y.Doc, write: () => void): boolean {
  if (!isEmpty(doc)) return false;
  const origin = doc.clientID;
  doc.clientID = SEED_CLIENT_ID;
  try {
    write();
  } finally {
    doc.clientID = origin;
  }
  return true;
}
