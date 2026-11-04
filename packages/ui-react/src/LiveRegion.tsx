/**
 * Saying what changed, to somebody who cannot see it change.
 *
 * A filtered count, a finished scan, a saved draft: all of them move
 * nothing and focus nothing, so a screen reader is told none of it.
 * The reference extension hand-rolled an `aria-live` paragraph with
 * `ui-visually-hidden`, which is the right shape and the wrong place
 * — every extension would write it, and most would get one of the
 * three details wrong.
 *
 * The three details:
 *
 * 1. **The region must exist before the text does.** A live region
 *    added to the DOM with its message already in it is frequently
 *    not announced at all — the assistive technology has nothing to
 *    observe a change against. So this renders an empty element and
 *    fills it afterwards.
 * 2. **The same message twice is not a change.** Scanning 40 files
 *    and then scanning 40 files again is silence, because the text
 *    never differed. A zero-width space is toggled on repeats.
 * 3. **Polite waits, assertive interrupts.** Almost everything is
 *    polite. Assertive is for something the user must act on, and
 *    using it for a count makes the product unusable by the people
 *    it is meant to help.
 */
import { useCallback, useEffect, useRef, useState } from "react";

/** Which way to say it. */
export type Urgency = "polite" | "assertive";

export interface Announcer {
  /** Say something. Re-saying the same thing still announces. */
  announce: (message: string, urgency?: Urgency) => void;
  /** Render this. It must be in the tree before anything is said. */
  region: React.ReactElement;
}

/**
 * A live region and the function that speaks into it.
 *
 * Two regions are rendered, not one. A single region whose
 * `aria-live` value changes is unreliable: the attribute is read
 * when the region is observed, so flipping it at the same moment the
 * text changes races, and the announcement comes out at whichever
 * urgency the implementation noticed first.
 */
export function useAnnounce(): Announcer {
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");
  const last = useRef({ polite: "", assertive: "" });

  const announce = useCallback(
    (message: string, urgency: Urgency = "polite") => {
      // Identical consecutive text is not a mutation, so nothing is
      // announced. A zero-width space makes it one, and is not read
      // aloud.
      const previous = last.current[urgency];
      const text = message === previous ? `${message}​` : message;
      last.current[urgency] = text;
      if (urgency === "assertive") setAssertive(text);
      else setPolite(text);
    },
    [],
  );

  const region = (
    <>
      <div
        className="ui-visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {polite}
      </div>
      <div
        className="ui-visually-hidden"
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
      >
        {assertive}
      </div>
    </>
  );

  return { announce, region };
}

export interface LiveRegionProps {
  /** What to say. Changing it announces; clearing it says nothing. */
  children?: string;
  urgency?: Urgency;
}

/**
 * The declarative form, for a value that already lives in state.
 *
 * `<LiveRegion>{`${n} results`}</LiveRegion>` is the whole usage.
 * Mounted empty on the first render so the region exists before it
 * has anything to say — see note 1 above.
 */
export function LiveRegion({ children = "", urgency = "polite" }: LiveRegionProps) {
  const [text, setText] = useState("");
  useEffect(() => {
    setText(children);
  }, [children]);
  return (
    <div
      className="ui-visually-hidden"
      role={urgency === "assertive" ? "alert" : "status"}
      aria-live={urgency}
      aria-atomic="true"
    >
      {text}
    </div>
  );
}
