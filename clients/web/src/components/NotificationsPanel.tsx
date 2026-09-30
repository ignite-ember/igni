/**
 * What is waiting on you, in a panel down the right.
 *
 * It was a page inside the artifacts panel, which made answering
 * "is anybody waiting on me" a navigation: leave whatever you were
 * doing, go to artifacts, find the page, and afterwards find your
 * way back. A question that gets asked between other pieces of work
 * should be answerable without abandoning them, so it opens over the
 * app and closes again, and what was underneath is still there.
 *
 * **It lists notifications, not comments.** It listed comments
 * once, which is why it could only ever say one kind of thing. Each
 * row is now a verb — commented, mentioned, asked to read, shared
 * with you — and opening it goes to the document, where the thing
 * can actually be answered.
 */
import { useCallback, useEffect, useState } from "react";

import { CloseIcon } from "./Icons";
import { NotificationList, type Notification } from "./NotificationList";
import type { IgniClient } from "../protocol/client";

export function NotificationsPanel({
  client,
  onOpenSlug,
  onClose,
}: {
  client: IgniClient;
  /** Open the document a notification is about, named by the slug
   *  the server sent. Closes this on the way — the notification was
   *  the reason for going, so leaving the panel over the document it
   *  pointed at would cover the answer. */
  onOpenSlug?: (slug: string) => void;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<Notification[]>([]);
  const [waiting, setWaiting] = useState(0);
  const [failed, setFailed] = useState("");

  const load = useCallback(async () => {
    try {
      const answer = await client.rpc<{
        notifications?: Notification[];
        waiting?: number;
        error?: string;
      }>("artifact_notifications", { unread_only: true });
      if (answer?.error) throw new Error(answer.error);
      setRows(answer?.notifications ?? []);
      setWaiting(answer?.waiting ?? 0);
      setFailed("");
    } catch (e) {
      // The list is left alone. Emptying it because one read failed
      // would say "nothing is waiting", which is the one answer this
      // panel must not give wrongly.
      setFailed(e instanceof Error ? e.message : String(e));
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  // And again whenever the server says something has happened. The
  // panel is open; the news should arrive in it rather than behind
  // it.
  useEffect(() => {
    return client.onEvent((message) => {
      const push = message as unknown as { channel?: string };
      if (push.channel !== "artifact_waiting") return;
      void load();
    });
  }, [client, load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      {/* Dimmed rather than blocked. What is underneath is still the
          thing being worked on, and a panel that hid it would make
          reading a comment about a document mean losing sight of the
          document. */}
      <div className="notify-backdrop" onClick={onClose} />
      <aside
        className="notify-panel"
        role="dialog"
        aria-label="Notifications"
        aria-modal="false"
      >
        <header className="notify-head">
          <h2 className="notify-title">Notifications</h2>
          <span className="notify-count">
            {waiting ? `${waiting} waiting` : "nothing waiting"}
          </span>
          {!!waiting && (
            <button
              type="button"
              className="btn btn-sm"
              title="Mark everything as seen"
              onClick={() => {
                void client
                  .rpc("artifact_mark_seen", {})
                  .then(() => load())
                  .catch(() => {});
              }}
            >
              Mark all seen
            </button>
          )}
          <button className="icon-btn" title="Close (Esc)" onClick={onClose}>
            <CloseIcon />
          </button>
        </header>
        <div className="notify-body">
          {failed && <p className="msg-error">{failed}</p>}
          <NotificationList
            rows={rows}
            onOpen={(slug, id) => {
              // Marked on the way out: opening it is what seeing it
              // means, and a row that stays bold after you have dealt
              // with it makes the count a number nobody believes.
              void client.rpc("artifact_mark_seen", { notification_id: id }).catch(() => {});
              onOpenSlug?.(slug);
              onClose();
            }}
          />
        </div>
      </aside>
    </>
  );
}
