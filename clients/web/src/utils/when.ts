/**
 * How long ago something happened, in as few characters as possible.
 *
 * Shared rather than copied. The document header and the comments
 * beside it both date the same events, and two implementations of
 * "how long ago" drift the moment one of them learns about weeks: a
 * version stamped "8d ago" next to a comment on it stamped "1w ago"
 * reads as two different days.
 *
 * Lives here rather than in the panel that first needed it because
 * the rail cannot import from that panel — the panel renders the
 * rail, and the import would close a cycle.
 */

/** A relative stamp: ``just now``, ``4m ago``, ``3d ago``. */
export function formatRelative(iso: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const secs = Math.floor((Date.now() - t) / 1000);
  // A clock that disagrees with the server's puts events a few
  // seconds in the future, and "in -3 seconds" is worse than the
  // small lie that it has just happened.
  if (secs < 60) return "just now";
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  return `${mo}mo ago`;
}

/**
 * The same moment in full, for a tooltip.
 *
 * "3d ago" is the right length for a rail and the wrong amount of
 * information for somebody working out what happened in what order.
 * The exact stamp rides along on hover rather than taking up room it
 * only occasionally earns.
 */
export function formatExact(iso: string): string {
  if (!iso) return "";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
