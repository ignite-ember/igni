/**
 * Two letters standing in for a face.
 *
 * Not an avatar. Nothing in the product stores an uploaded picture —
 * there is no `avatar_url` on a user anywhere in the server — and a
 * generic silhouette for everybody says strictly less than two
 * letters of somebody's own name. The portal draws the same thing
 * from the same source, so the two surfaces agree about what a person
 * looks like.
 *
 * The colour is derived from the name rather than chosen, so the same
 * person is the same colour on every machine and across restarts.
 * That is the whole point: the monogram is a recognition aid, and one
 * that changed colour per session would be decoration.
 */

export function initialsOf(name: string): string {
  const cleaned = (name || "").trim();
  if (!cleaned) return "?";

  // An address is not a name: "maya@acme.test" should read as M, not
  // as MA from "maya" and "acme".
  const local = cleaned.includes("@") ? cleaned.split("@")[0] : cleaned;
  const words = local.split(/[\s._-]+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** A stable hue for a name. Same person, same colour, every time. */
export function hueOf(name: string): number {
  let hash = 0;
  for (const ch of name || "") {
    hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  }
  return hash;
}

export function Monogram({ name, size = 18 }: { name: string; size?: number }) {
  const hue = hueOf(name);
  return (
    <span
      className="monogram"
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.46),
        // Fixed saturation and lightness so every generated colour
        // clears contrast against white text, rather than hoping the
        // hash lands somewhere readable.
        background: `hsl(${hue} 42% 42%)`,
      }}
    >
      {initialsOf(name)}
    </span>
  );
}
