/** Inline SVG icons — UI chrome never uses emoji glyphs. */

interface IconProps {
  size?: number;
}

const strokeProps = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

function Svg({ size = 16, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0 }}
      {...strokeProps}
    >
      {children}
    </svg>
  );
}

/** Brand flame — same artwork as the landing page favicon. */
export function FlameIcon({ size = 16 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0 }}
    >
      <path
        d="M7.998 14.5c-3.312 0-6-2.246-6-5.014 0-3.047 2.262-5.674 3.625-7.018.27-.266.717-.058.717.318 0 1.335.877 2.562 1.753 2.562.645 0 .972-.379 1.096-.65.258-.564.2-1.363.027-2.043-.098-.387.3-.726.65-.498C10.87 3.534 14 6.186 14 9.486c0 2.768-2.688 5.014-6.002 5.014z"
        fill="#f85149"
      />
      <path
        d="M8.7 13.563c-1.822 0-3.3-1.235-3.3-2.758 0-1.677 1.244-3.122 1.994-3.862.149-.146.394-.032.394.175 0 .734.482 1.41.964 1.41.355 0 .534-.209.603-.358.142-.31.11-.75.015-1.124-.054-.213.165-.4.357-.274 1.266.894 2.987 2.354 2.987 4.033 0 1.523-1.479 2.758-3.014 2.758z"
        fill="#f0883e"
      />
    </svg>
  );
}

export function FolderIcon({ size = 13 }: IconProps) {
  return (
    <Svg size={size}>
      <path d="M1.8 4.2c0-.7.5-1.2 1.2-1.2h2.6l1.5 1.6h5.9c.7 0 1.2.5 1.2 1.2v6c0 .7-.5 1.2-1.2 1.2H3c-.7 0-1.2-.5-1.2-1.2v-7.6z" />
    </Svg>
  );
}

export function CloudIcon({ size = 13 }: IconProps) {
  return (
    <Svg size={size}>
      <path d="M4.5 12.5a3 3 0 0 1-.4-5.97 4 4 0 0 1 7.82.97h.33a2.5 2.5 0 0 1 0 5h-7.75z" />
    </Svg>
  );
}

export function MenuIcon({ size = 16 }: IconProps) {
  return (
    <Svg size={size}>
      <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
    </Svg>
  );
}

export function CloseIcon({ size = 14 }: IconProps) {
  return (
    <Svg size={size}>
      <path d="M4 4l8 8M12 4l-8 8" />
    </Svg>
  );
}

export function PencilIcon({ size = 12 }: IconProps) {
  return (
    <Svg size={size}>
      <path d="M9.8 2.7l3.5 3.5L5.5 14H2v-3.5l7.8-7.8zM8.5 4l3.5 3.5" />
    </Svg>
  );
}

export function ArrowUpIcon({ size = 14 }: IconProps) {
  return (
    <Svg size={size}>
      <path d="M8 13V3M3.8 7.2L8 3l4.2 4.2" />
    </Svg>
  );
}

export function StopIcon({ size = 12 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0 }}
    >
      <rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor" />
    </svg>
  );
}

/* ── Status glyphs (run / phase / agent state) ────────────────
   Each is a small SVG that takes ``currentColor`` so the
   colour comes from the host element. Filled (not stroked)
   for visual weight parity with the team-progress card's
   agent status dot. */

export function CheckIcon({ size = 10 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" }}
    >
      <path d="M3 8.5l3.2 3.2L13 4.8" />
    </svg>
  );
}

export function XIcon({ size = 10 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0, stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" }}
    >
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

export function CircleIcon({ size = 8, filled = false }: IconProps & { filled?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0, fill: filled ? "currentColor" : "none", stroke: "currentColor", strokeWidth: 2 }}
    >
      <circle cx="8" cy="8" r="5" />
    </svg>
  );
}

export function CancelIcon({ size = 10 }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" }}
    >
      <circle cx="8" cy="8" r="5.5" />
      <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" />
    </svg>
  );
}

export function ClockIcon({ size = 10 }: IconProps) {
  return (
    <Svg size={size}>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5.2V8l1.8 1.5" />
    </Svg>
  );
}

/** Pick the right status icon for a workflow run / phase / agent
 *  status string. The host element controls the colour via
 *  ``currentColor``. Returns ``null`` for unknown statuses so
 *  callers can render a fallback dot. */
export function StatusIcon({
  status,
  size = 10,
}: {
  status: "running" | "completed" | "failed" | "cancelled" | "timeout" | string;
  size?: number;
}) {
  switch (status) {
    case "running":
      return <CircleIcon size={size} filled />;
    case "completed":
      return <CheckIcon size={size} />;
    case "failed":
      return <XIcon size={size} />;
    case "cancelled":
      return <CancelIcon size={size} />;
    case "timeout":
      return <ClockIcon size={size} />;
    default:
      return <CircleIcon size={size} />;
  }
}

/** Right-pointing chevron; rotate via the host element (e.g.
 * `.tool-chevron.open`) or the `down` prop. */
export function ChevronIcon({ size = 10, down = false }: IconProps & { down?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      style={{
        display: "block",
        flexShrink: 0,
        transform: down ? "rotate(90deg)" : undefined,
      }}
      {...strokeProps}
      strokeWidth={2}
    >
      <path d="M5.5 2.8L10.7 8l-5.2 5.2" />
    </svg>
  );
}
