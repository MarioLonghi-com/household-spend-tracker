/**
 * "Updated with changes made elsewhere", for a few seconds after a refresh.
 *
 * Rows moving under the cursor with no word of why read as the app misbehaving
 * (#369). This says it once, politely to a screen reader, and goes; nothing
 * is clickable because there is nothing to decide -- the register's panel is
 * where an edit that collided is dealt with.
 */

import { useEffect, useState } from "react";
import { Trans } from "@lingui/react/macro";

/** How long the notice stays. */
export const SHOWN_MS = 6_000;

export function UpdatedNotice({ at }: { at: Date | null }) {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!at) return;
    setShown(true);
    const timer = window.setTimeout(() => setShown(false), SHOWN_MS);
    return () => window.clearTimeout(timer);
  }, [at]);

  return (
    // A live region without `role="status"`: the shell is on every screen,
    // and a second status region would be one more for anything on a screen
    // that announces its own ("Saved.") to be confused with.
    <div className="updated-notice" aria-live="polite">
      {shown ? (
        <span className="small muted">
          <Trans>Updated just now with changes made elsewhere.</Trans>
        </span>
      ) : null}
    </div>
  );
}
