import ReactMarkdown from "react-markdown";
import { Drawer } from "./Drawer";

/**
 * Generic markdown panel for /agents, /skills, /plugins, /artifacts,
 * /hooks — the BE CommandHandler already renders rich markdown for
 * these; the TUI shows bespoke widgets, the web shows the markdown.
 */
export function InfoPanel({
  title,
  markdown,
  onClose,
}: {
  title: string;
  markdown: string;
  onClose: () => void;
}) {
  return (
    <Drawer title={title} onClose={onClose}>
      <div className="msg-assistant">
        <ReactMarkdown>{markdown || "_No content._"}</ReactMarkdown>
      </div>
    </Drawer>
  );
}
