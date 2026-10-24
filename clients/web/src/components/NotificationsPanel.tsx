/**
 * What is waiting on you, in a panel down the right.
 *
 * It opens over the app and closes again, and what was underneath is
 * still there: "is anybody waiting on me" gets asked between other
 * pieces of work and should be answerable without abandoning them.
 *
 * **The floor's list, not an extension's.** It used to ask Artifacts
 * for its comment queue. It now asks igni's backend, which merges what
 * igni server delivered to whoever is signed in with what extensions
 * on this machine told it (igni server's `docs/service-identity.md`
 * §10) — one list, every extension, each row saying which one it is
 * from. Opening a row goes to the extension page it names, with the
 * params it carries, and marks it read where it lives: on igni server
 * for the one, on this machine for the other. The backend knows which.
 */
import { useCallback, useEffect, useState } from "react";

import { CloseIcon } from "./Icons";
import { NotificationList } from "./NotificationList";
import {
  countsFromPush,
  isCounts,
  type FloorNotification,
  type NotificationCounts,
  type NotificationTarget,
} from "../notifications/model";
import type { IgniClient } from "../protocol/client";

export function NotificationsPanel({
  client,
  sourceName,
  canOpen,
  onOpenTarget,
  onCountsChanged,
  onClose,
}: {
  client: IgniClient;
  /** The extension's name, for its id. */
  sourceName?: (source: string) => string;
  /** Whether this app has a page called `page` right now. */
  canOpen?: (page: string) => boolean;
  /** Go to the page a notification names. The panel closes on the way:
   *  the notification was the reason for going, and leaving the panel
   *  over the page it pointed at would cover the answer. */
  onOpenTarget?: (target: NotificationTarget, row: FloorNotification) => void;
  /** The counts, every time this learns them — marking something read
   *  moves every badge, not only this list. */
  onCountsChanged?: (counts: NotificationCounts) => void;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<FloorNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [failed, setFailed] = useState("");

  const learned = useCallback(
    (counts: unknown) => {
      if (!isCounts(counts)) return;
      setUnread(counts.unread);
      onCountsChanged?.(counts);
    },
    [onCountsChanged],
  );

  const load = useCallback(async () => {
    try {
      const answer = await client.rpc<{
        notifications?: FloorNotification[];
        counts?: NotificationCounts;
        server_error?: string;
        error?: string;
      }>("notifications_list");
      if (answer?.error) throw new Error(answer.error);
      setRows(answer?.notifications ?? []);
      learned(answer?.counts);
      // The local half and the last rows read are still shown: "igni
      // server could not be asked" is not "nothing is waiting".
      setFailed(answer?.server_error ?? "");
    } catch (e) {
      // The list is left alone. Emptying it because one read failed
      // would say "nothing is waiting", which is the one answer this
      // panel must not give wrongly.
      setFailed(e instanceof Error ? e.message : String(e));
    }
  }, [client, learned]);

  useEffect(() => {
    void load();
  }, [load]);

  // And again whenever the counts move. The panel is open; the news
  // should arrive in it rather than behind it.
  useEffect(() => {
    return client.onEvent((message) => {
      if (countsFromPush(message)) void load();
    });
  }, [client, load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const markRead = useCallback(
    async (args: Record<string, unknown>) => {
      try {
        const answer = await client.rpc<{ counts?: NotificationCounts }>(
          "notifications_mark_read",
          args,
        );
        learned(answer?.counts);
      } catch {
        // Reading it again below shows whatever is actually true.
      }
    },
    [client, learned],
  );

  return (
    <>
      {/* Dimmed rather than blocked. What is underneath is still the
          thing being worked on. */}
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
            {unread ? `${unread} unread` : "nothing unread"}
          </span>
          {!!unread && (
            <button
              type="button"
              className="btn btn-sm"
              title="Mark everything as read"
              onClick={() => {
                void markRead({}).then(() => load());
              }}
            >
              Mark all read
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
            sourceName={sourceName}
            canOpen={(row) => !!row.target && (canOpen?.(row.target.page) ?? true)}
            onOpen={(row) => {
              if (!row.target) return;
              // Marked on the way out: opening it is what reading it
              // means, and a row that stays bold after you have dealt
              // with it makes the count a number nobody believes.
              if (!row.read) void markRead({ ids: [row.id] });
              onOpenTarget?.(row.target, row);
              onClose();
            }}
          />
        </div>
      </aside>
    </>
  );
}
