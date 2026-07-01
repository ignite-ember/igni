/** Inline SVG file-type icon for the inline file pill. One glyph per
 *  category — picks by the filename's extension. Color is set by the
 *  parent (currentColor) so the icon inherits the pill's tint. */

export type Kind = "image" | "pdf" | "code" | "data" | "doc" | "archive" | "video" | "audio" | "shell" | "file";

const KINDS: Record<string, Kind> = {
  // images
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
  svg: "image", bmp: "image", ico: "image", heic: "image", avif: "image",
  // documents
  pdf: "pdf",
  doc: "doc", docx: "doc", odt: "doc", rtf: "doc",
  txt: "doc", md: "doc", rst: "doc", log: "doc",
  // code
  js: "code", jsx: "code", ts: "code", tsx: "code", py: "code", rb: "code",
  go: "code", rs: "code", java: "code", c: "code", h: "code", cpp: "code",
  cc: "code", hpp: "code", cs: "code", swift: "code", kt: "code", scala: "code",
  php: "code", lua: "code", r: "code", vue: "code", svelte: "code",
  html: "code", htm: "code", css: "code", scss: "code", sass: "code", less: "code",
  // data
  json: "data", yaml: "data", yml: "data", toml: "data", ini: "data",
  csv: "data", tsv: "data", xml: "data", sql: "data", env: "data",
  // archives
  zip: "archive", tar: "archive", gz: "archive", tgz: "archive",
  rar: "archive", "7z": "archive", bz2: "archive", xz: "archive",
  // video / audio
  mp4: "video", mov: "video", avi: "video", mkv: "video", webm: "video",
  mp3: "audio", wav: "audio", ogg: "audio", flac: "audio", m4a: "audio",
  // shell
  sh: "shell", bash: "shell", zsh: "shell", fish: "shell", ps1: "shell",
};

export function kindFor(name: string): Kind {
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return KINDS[ext] || "file";
}

interface Props {
  name: string;
  size?: number;
}

export function FileTypeIcon({ name, size = 16 }: Props) {
  const kind = kindFor(name);
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      style={{ display: "block", flexShrink: 0 }}
    >
      {/* Document page with a folded corner — shared shape so the
          icons feel consistent. */}
      <path
        d="M3.5 1.5h6L13 5v8a1.5 1.5 0 0 1-1.5 1.5h-8A1.5 1.5 0 0 1 2 13V3a1.5 1.5 0 0 1 1.5-1.5z"
        fill="currentColor"
        opacity="0.18"
      />
      <path
        d="M3.5 1.5h6L13 5v8a1.5 1.5 0 0 1-1.5 1.5h-8A1.5 1.5 0 0 1 2 13V3a1.5 1.5 0 0 1 1.5-1.5z"
        stroke="currentColor"
        strokeWidth="1"
      />
      <path d="M9.5 1.5V5H13" stroke="currentColor" strokeWidth="1" fill="none" />
      <KindGlyph kind={kind} />
    </svg>
  );
}

function KindGlyph({ kind }: { kind: Kind }) {
  // Inner mark, painted in solid currentColor so the pill's tint
  // carries through. Positioned in the lower half of the page so the
  // folded corner stays clean.
  switch (kind) {
    case "image":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none" strokeLinecap="round" strokeLinejoin="round">
          <rect x="4" y="8" width="7" height="4.5" rx="0.6" />
          <circle cx="5.5" cy="9.5" r="0.6" fill="currentColor" />
          <path d="M4.2 12.2L6 10.5l1.6 1.4 1.4-1.1 1.6 1.4" />
        </g>
      );
    case "pdf":
      return (
        <g fill="currentColor">
          <text x="8" y="12.7" fontSize="4.6" fontWeight="700" textAnchor="middle" fontFamily="ui-sans-serif, system-ui">
            PDF
          </text>
        </g>
      );
    case "code":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 9.5L4.4 11 6 12.5" />
          <path d="M10 9.5L11.6 11 10 12.5" />
          <path d="M8.8 9L7.2 13" />
        </g>
      );
    case "data":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none">
          <line x1="4" y1="9" x2="11" y2="9" />
          <line x1="4" y1="10.6" x2="11" y2="10.6" />
          <line x1="4" y1="12.2" x2="9" y2="12.2" />
        </g>
      );
    case "archive":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none" strokeLinecap="round">
          <line x1="8" y1="8.5" x2="8" y2="13" strokeDasharray="0.9 0.7" />
        </g>
      );
    case "video":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none" strokeLinecap="round" strokeLinejoin="round">
          <rect x="4" y="9" width="7" height="4" rx="0.6" />
          <path d="M6.5 10v2l2-1z" fill="currentColor" stroke="none" />
        </g>
      );
    case "audio":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 12.4V9.5l4-0.8v2.9" />
          <circle cx="5.4" cy="12.5" r="0.7" fill="currentColor" stroke="none" />
          <circle cx="9.4" cy="11.6" r="0.7" fill="currentColor" stroke="none" />
        </g>
      );
    case "shell":
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4.5 10L6 11.2 4.5 12.4" />
          <line x1="7.5" y1="12.5" x2="11" y2="12.5" />
        </g>
      );
    case "doc":
    case "file":
    default:
      return (
        <g stroke="currentColor" strokeWidth="0.9" fill="none">
          <line x1="4" y1="9" x2="11" y2="9" />
          <line x1="4" y1="10.6" x2="11" y2="10.6" />
          <line x1="4" y1="12.2" x2="9" y2="12.2" />
        </g>
      );
  }
}
