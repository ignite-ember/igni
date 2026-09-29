/**
 * Who is in the document on screen, kept current.
 *
 * Two calls and a subscription. ``artifact_watch_room`` asks this
 * machine's backend to hold a place in the document's room; the
 * roster comes back on the push channel the backend already has to
 * this view; leaving happens when the document changes or the panel
 * goes away.
 *
 * **The socket is not here.** It would be the obvious place for it —
 * this is the code that wants the answer — and it would mean the
 * cloud token in the browser to open it with. Every other cloud call
 * in this product goes through the local backend, which is where the
 * credentials are, so this one does too and the token stays put.
 *
 * The cost is one hop, and one real consequence: presence follows the
 * *backend's* connection, not the tab's. Close the laptop and the
 * backend goes with it, so the room empties correctly. Leave the tab
 * open with the backend running and you stay present, which is also
 * true — the document is still open.
 */
import { useEffect, useState } from "react";

import type { IgniClient } from "../../protocol/client";
import type { Occupant, Roster } from "./DocumentPeople";

interface RoomPush {
  channel?: string;
  payload?: { artifact_id?: string; here?: Occupant[] };
}

export interface DocumentRoom {
  roster: Roster | null;
  here: Occupant[];
}

export function useDocumentRoom(
  client: IgniClient,
  artifactId: string,
  /** Bumped by the caller to re-read the roster after a grant. */
  revision = 0,
): DocumentRoom {
  const [roster, setRoster] = useState<Roster | null>(null);
  const [here, setHere] = useState<Occupant[]>([]);

  // The roster: a fact about the document, so it is fetched when the
  // document changes and when somebody has just changed who can see
  // it. Not on a timer — it does not drift on its own.
  useEffect(() => {
    if (!artifactId) {
      setRoster(null);
      return;
    }
    let dropped = false;
    void client
      .rpc<Roster>("artifact_people", { artifact_id: artifactId })
      .then((answer) => {
        if (!dropped) setRoster(answer ?? null);
      })
      .catch(() => {
        // A document that was never shared, or a server that cannot
        // be reached. Both mean there is nothing to draw, and neither
        // is worth a message under the title of a document that is
        // otherwise working.
        if (!dropped) setRoster(null);
      });
    return () => {
      dropped = true;
    };
  }, [client, artifactId, revision]);

  // Presence: held open for as long as this document is on screen.
  useEffect(() => {
    if (!artifactId) return;
    setHere([]);
    void client.rpc("artifact_watch_room", { artifact_id: artifactId }).catch(() => {
      /* No server, no room. The document still opens. */
    });
    return () => {
      // On the way out, not on the way in. Leaving as the next
      // document arrives would be a race with its own join, and the
      // backend only holds one room at a time anyway — so the join
      // does the leaving and this covers closing the panel.
      void client.rpc("artifact_leave_room", {}).catch(() => {});
    };
  }, [client, artifactId]);

  useEffect(() => {
    return client.onEvent((message) => {
      const push = message as unknown as RoomPush;
      if (push.channel !== "artifact_room") return;
      // Checked here as well as on the backend. A roster for the
      // document that was open a moment ago would arrive after this
      // one had drawn its own, and replace it with strangers.
      if (push.payload?.artifact_id !== artifactId) return;
      setHere(push.payload?.here ?? []);
    });
  }, [client, artifactId]);

  return { roster, here };
}
