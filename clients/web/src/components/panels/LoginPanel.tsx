import { useEffect, useState } from "react";
import type { EmberClient } from "../../protocol/client";

/**
 * Browser-callback login. Progress streams in as push notifications:
 * `login_status` (text updates) then one `login_result`.
 */
export function LoginPanel({
  client,
  onDone,
}: {
  client: EmberClient;
  onDone: (success: boolean, detail: string) => void;
}) {
  const [statusText, setStatusText] = useState("Starting login…");

  useEffect(() => {
    const off = client.onEvent((m) => {
      if (m.type !== "push_notification") return;
      if (m.channel === "login_status") {
        setStatusText(String(m.payload.text ?? ""));
      } else if (m.channel === "login_result") {
        onDone(Boolean(m.payload.success), String(m.payload.result ?? ""));
      }
    });
    client.login().catch((e) => onDone(false, String(e)));
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="overlay">
      <div className="dialog">
        <div className="dialog-title">Log in to igni</div>
        <div className="dialog-sub" style={{ whiteSpace: "pre-wrap" }}>
          {statusText}
        </div>
        <div className="dialog-actions">
          <button
            className="btn btn-danger"
            onClick={() => {
              client.cancelLogin();
              onDone(false, "cancelled");
            }}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
