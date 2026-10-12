/**
 * What is waiting on you, whatever kind of thing it is.
 *
 * This used to be the comment queue with a new title. It read well
 * enough — a comment is the commonest thing to be waiting on — and
 * it could say nothing else: somebody asking to be let into a
 * document is exactly as much waiting on you, and there was no line
 * for it.
 *
 * So each row is a **verb** and the things it happened to. The verb
 * decides the wording, which is the whole reason the server sends
 * one rather than a rendered sentence: "asked to read" and "left a
 * comment on" are the same shape and want different words, and a
 * server writing English would be a server that has to be redeployed
 * to change a preposition.
 *
 * **Reading is not doing.** Opening the document is what this offers;
 * answering the comment or granting the access happens there, next
 * to the thing it is about. A queue that also did the work would be
 * a second place to do everything, quietly disagreeing with the
 * first.
 */
import { MenuIcon, type MenuIconName } from "./MenuIcon";
import { Monogram } from "./Monogram";

export interface Notification {
  id: string;
  kind: string;
  actor_email?: string;
  slug?: string;
  title?: string;
  subject_id?: string;
  body?: string;
  created_at?: string;
  read?: boolean;
}

/** How each verb reads, and what it is drawn with. */
const VERBS: Record<string, { says: string; icon: MenuIconName }> = {
  comment: { says: "commented on", icon: "quote" },
  reply: { says: "replied in", icon: "quote" },
  mention: { says: "mentioned you in", icon: "quote" },
  access_requested: { says: "asked to read", icon: "lock" },
  access_granted: { says: "let you into", icon: "unlock" },
  access_declined: { says: "did not let you into", icon: "lock" },
  version_published: { says: "published a version of", icon: "document" },
  shared_with_you: { says: "shared", icon: "share" },
};

/** A verb this build has not heard of still has to read as something.
 *
 *  A deployment one version ahead will send kinds that did not exist
 *  when this shipped, and a row that renders as nothing is a
 *  notification the reader never learns about — worse than a plain
 *  line saying something happened. */
const UNKNOWN = { says: "did something on", icon: "document" as MenuIconName };

export function when(iso: string): string {
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
  onOpen,
}: {
  rows: Notification[];
  /** Go to the document it is about. */
  onOpen?: (slug: string, notificationId: string) => void;
}) {
  if (!rows.length) {
    return <p className="notify-empty">Nothing is waiting on you.</p>;
  }
  return (
    <ul className="notify-rows">
      {rows.map((row) => {
        const verb = VERBS[row.kind] ?? UNKNOWN;
        const who = row.actor_email || "Somebody";
        return (
          <li key={row.id} className={row.read ? "notify-row is-read" : "notify-row"}>
            <button
              type="button"
              className="notify-open"
              disabled={!row.slug}
              title={row.slug ? "Open the document" : "This document is gone"}
              onClick={() => row.slug && onOpen?.(row.slug, row.id)}
            >
              <span className="notify-face">
                <Monogram name={who} size={22} />
                <span className="notify-verb-icon" aria-hidden="true">
                  <MenuIcon name={verb.icon} />
                </span>
              </span>
              <span className="notify-said">
                <span className="notify-line">
                  <strong>{who}</strong> {verb.says}{" "}
                  <strong>{row.title || "a document"}</strong>
                </span>
                {row.body && <span className="notify-excerpt">{row.body}</span>}
                <span className="notify-when">{when(row.created_at ?? "")}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
