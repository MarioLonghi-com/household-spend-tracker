/**
 * A value that follows another once it has stopped changing.
 *
 * The register's search box went straight into its query key, so typing
 * "tesco" asked the server for the whole filtered register five times -- up to
 * 25,000 rows each -- and four of the answers were thrown away on arrival. The
 * key now follows this instead: one request per pause, not one per keystroke.
 *
 * Emptying the box is taken at once rather than after the pause. "Clear
 * filters" and a select-all-delete are one decision, not a word being typed,
 * and waiting a quarter-second to show the unfiltered list reads as lag.
 */

import { useEffect, useState } from "react";

/** Long enough to cover a word typed at speed, short enough not to feel slow. */
export const DEBOUNCE_MS = 250;

export function useDebounced(value: string, delay: number = DEBOUNCE_MS): string {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    if (value.trim() === "") {
      setSettled(value);
      return;
    }
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return settled;
}
