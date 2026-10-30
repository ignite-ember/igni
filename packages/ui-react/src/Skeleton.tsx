import type { CSSProperties } from "react";

/**
 * Animated placeholder block. Render at the same approximate size
 * and rhythm as the eventual content so the layout doesn't jump
 * when async data lands.
 *
 * Usage:
 *   <Skeleton.Line width="60%" />
 *   <Skeleton.Block height={64} />
 *   <Skeleton.Circle size={48} />
 */
export const Skeleton = {
  Line({
    width = "100%",
    height = 12,
    style,
  }: {
    width?: number | string;
    height?: number | string;
    style?: CSSProperties;
  }) {
    return (
      <span
        className="skeleton skeleton-text"
        style={{ display: "block", width, height, ...style }}
      />
    );
  },
  Block({
    height = 64,
    width = "100%",
    style,
  }: {
    height?: number | string;
    width?: number | string;
    style?: CSSProperties;
  }) {
    return (
      <span
        className="skeleton skeleton-block"
        style={{ display: "block", height, width, ...style }}
      />
    );
  },
  Circle({ size = 24, style }: { size?: number; style?: CSSProperties }) {
    return (
      <span
        className="skeleton skeleton-circle"
        style={{ display: "inline-block", width: size, height: size, ...style }}
      />
    );
  },
  Rows({ count = 4, style }: { count?: number; style?: CSSProperties }) {
    return (
      <div
        style={{ display: "flex", flexDirection: "column", gap: 12, ...style }}
      >
        {Array.from({ length: count }).map((_, i) => (
          <div key={i}>
            <Skeleton.Line width="40%" height={11} />
            <Skeleton.Line width="100%" />
          </div>
        ))}
      </div>
    );
  },
};
