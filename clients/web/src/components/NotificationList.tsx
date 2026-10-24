/**
 * Notifications, whichever extension they are from.
 *
 * This was the Artifacts comment queue with a verb table: it knew what
 * "access_requested" meant and how to say it. It cannot know that any
 * more — notifications are the floor's (igni server's
 * `docs/service-identity.md` §10), every extension has its own kinds,
 * and a host that kept a vocabulary for each would be a host that has
 * to ship whenever an extension does. So each row says what the
 * notification itself says: which extension it is from (and which
 * organisation, for somebody in more than one), its title, its line of
 * body, who did it, and when.
 *
 * **Reading is not doing.** Opening a row goes to the extension page it
 * names, where the thing can be answered. A queue that also did the
 * work would be a second place to do everything, quietly disagreeing
 * with the first.
 */
import { Monogram } from "@igni/ui-react";

import type { FloorNotification } from "../notifications/model";

export type { FloorNotification };

export function when(iso: string | null | undefined): string {
  const at = Date.parse(iso || "");
  if (Number.isNaN(at)) return "";
  const seconds = Math.max(0, (Date.now() - at) / 1000);
  if (seconds < 90) return "just now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function NotificationList({
  rows,
  sourceName = (id) => id,
  canOpen = () => true,
  onOpen,
}: {
  rows: FloorNotification[];
  /** The extension's name, for its id. Its id when it is not installed. */
  sourceName?: (source: string) => string;
  /** Whether this app has the page a notification points at. */
  canOpen?: (row: FloorNotification) => boolean;
  /** Go where it points. */
  onOpen?: (row: FloorNotification) => void;
}) {
  if (!rows.length) {
    return <p className="notify-empty">Nothing is waiting on you.</p>;
  }
  return (
    <ul className="notify-rows">
      {rows.map((row) => {
        const from = sourceName(row.source);
        const openable = row.target !== null && canOpen(row);
        return (
          <li key={`${row.origin}:${row.id}`} className={row.read ? "notify-row is-read" : "notify-row"}>
            <button
              type="button"
              className="notify-open"
              disabled={!openable}
              title={
                openable
                  ? `Open in ${from}`
                  : `${from} is not installed here, so this cannot be opened`
              }
              onClick={() => openable && onOpen?.(row)}
            >
              <span className="notify-face">
                <Monogram name={row.actor || from} size={22} />
              </span>
              <span className="notify-said">
                <span className="notify-source">
                  {from}
                  {/* Which organisation, when there is more than one to
                      come from: the backend names it only then. */}
                  {row.org_name && <span className="notify-org"> · {row.org_name}</span>}
                </span>
                <span className="notify-line">
                  <strong>{row.title || row.kind}</strong>
                </span>
                {row.body && <span className="notify-excerpt">{row.body}</span>}
                <span className="notify-when">
                  {row.actor && <span className="notify-actor">{row.actor}</span>}
                  {row.actor && row.created_at && " · "}
                  {when(row.created_at)}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
