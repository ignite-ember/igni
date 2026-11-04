import type { HITLRequest, RequirementDetails } from "../protocol/messages";

/** The open approvals with one requirement's reason filled in.
 *
 *  The Bypass safety check raises its dialog with a spinner, then sends
 *  the model's description of the call. Only that line changes: the
 *  dialog, its order and any answers already given stay. An update for
 *  a requirement no longer open is dropped. */
export function withDetails(
  open: HITLRequest[] | null,
  update: Pick<RequirementDetails, "requirement_id" | "details">,
): HITLRequest[] | null {
  if (!open) return open;
  if (!open.some((r) => r.requirement_id === update.requirement_id)) return open;
  return open.map((r) =>
    r.requirement_id === update.requirement_id
      ? { ...r, details: update.details, details_pending: false }
      : r,
  );
}
