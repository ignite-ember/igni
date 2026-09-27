import type { VersionDiff } from "./ArtifactDiffTable";

/**
 * The banner on a document whose foundation has moved.
 *
 * Revising a document produces v6 after v5 the way a commit follows a
 * commit on the branch you are working on — nothing has drifted. What
 * matters is the *other* branch: the document this one was built on,
 * and whether it has moved since we branched from it.
 *
 * The banner says that, and leads to the review. It does not answer
 * anything itself: a comparison and a report unfolding inside the
 * document being reviewed pushes the thing under review off the
 * screen, and leaves no way back to a decision once you have scrolled
 * past it. See :mod:`DriftReviewPage`.
 */

export interface DriftAffected {
  text: string;
  line_from: number;
  line_to: number;
  source_text: string;
  strength: string;
}

export interface Drift {
  dependent_name: string;
  source_artifact_id: string;
  source_name: string;
  pinned_n: number;
  source_head_n: number;
  versions_behind: number;
  summary: string;
  affected: DriftAffected[];
  /** The turn that asks what the drift means. Empty if nothing moved. */
  prompt: string;
  error: string;
}

/** One confirmed source that has moved, as the lineage reports it. */
export interface StaleSource {
  artifact_id: string;
  name: string;
  pinned_n: number;
  head_n: number;
}

export interface DriftState {
  drift: Drift | null;
  verdict: string;
  thinking: string;
  diff: VersionDiff | null;
  busy: "" | "landed" | "meaning";
  error: string;
}

/**
 * The banner: where we branched, and the way in.
 *
 * It used to say "Sources have changed since this was written" and
 * stop — naming neither the source, nor how far it had gone, nor
 * anything you could do about it.
 *
 * One button, not two. There were briefly two — "What landed" and
 * "What does it mean here?" — which opened the same page and differed
 * only in whether the report started on arrival. That is not a
 * difference a reader can see before clicking, so it was not a choice
 * so much as a coin toss with two labels.
 */
export function DriftBanner({
  sources,
  onReview,
}: {
  sources: StaleSource[];
  onReview: () => void;
}) {
  if (!sources.length) return null;
  const first = sources[0];
  const behind = Math.max(first.head_n - first.pinned_n, 0);

  return (
    <div className="kb-drift-banner" role="status">
      <div className="kb-drift-banner-text">
        <strong>
          {first.name || first.artifact_id} has moved on {behind} version
          {behind === 1 ? "" : "s"} since this was written.
        </strong>
        <span className="kb-drift-banner-detail">
          Branched from v{first.pinned_n}; it is now at v{first.head_n}.
          {sources.length > 1 ? ` ${sources.length - 1} other source also moved.` : ""}
        </span>
      </div>

      <div className="kb-drift-banner-actions">
        <button type="button" className="btn btn-sm btn-primary" onClick={onReview}>
          What landed
        </button>
      </div>
    </div>
  );
}

