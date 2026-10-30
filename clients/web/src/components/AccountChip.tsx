import { Monogram } from "@igni/ui-react";

import type { StatusUpdate } from "../protocol/messages";
import { ChevronIcon } from "./Icons";

/** The header's account chip, or "Log in" when nobody is signed in.
 *
 *  Who is signed in is `cloud_email`, the address on disk, which is
 *  what the sign-in wall goes by too. `cloud_connected` is whether the
 *  session holds a live token for the igni server: an air-gapped,
 *  on-prem install can lack one while the person is signed in, so it
 *  never decides between the chip and "Log in". It is said on the
 *  chip's title instead. Nothing is shown until the first status
 *  arrives, rather than "Log in" flashing at somebody signed in. */
export function AccountChip({
  status,
  name,
  onOpenAccount,
  onLogIn,
}: {
  status: Pick<StatusUpdate, "cloud_email" | "cloud_connected"> | null;
  name: string;
  onOpenAccount: () => void;
  onLogIn: () => void;
}) {
  if (status === null) return null;
  if (status.cloud_email) {
    return (
      <button
        className="chip"
        title={
          status.cloud_connected ? "Account" : "Account: not connected to the igni server"
        }
        onClick={onOpenAccount}
      >
        {/* A monogram, not an avatar: nothing in the product stores an
            uploaded picture, and inventing a silhouette for everybody
            says less than two letters of their own name. The portal
            draws the same thing from the same source. */}
        <Monogram name={name} /> <span className="chip-label">{name}</span>{" "}
        <ChevronIcon size={9} down />
      </button>
    );
  }
  return (
    <button
      className="chip"
      style={{ background: "var(--ember-gradient)", color: "#fff", border: "none" }}
      onClick={onLogIn}
    >
      Log in
    </button>
  );
}
