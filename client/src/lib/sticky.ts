/**
 * A screen's settings as the person last left them (#142, #143).
 *
 * ## Per browser, per household, never the database
 *
 * The owner asked for this explicitly: which filters were on when you last looked
 * is a fact about the device you looked on, like the appearance and the
 * column widths, not about the ledger. So it lives in `localStorage`, keyed by
 * screen *and* household -- the accounts ticked in one household are ids that
 * mean nothing in the other.
 *
 * ## One key per setting
 *
 * `spendtracker.<screen>.<household>.<name>`. A setting whose shape changes
 * in a later version costs that setting, not the screen's others: every read
 * goes through `accept`, and whatever it refuses reads as the default.
 *
 * Every read and write is in a `try` -- a private window throws, and the
 * screen has to draw anyway.
 */

import { useCallback, useState } from "react";

export function stickyKey(screen: string, household: string, name: string): string {
  return `spendtracker.${screen}.${household}.${name}`;
}

/**
 * What is stored, or `undefined` when there is nothing usable.
 *
 * Without an `accept`, a value is kept only when it has the default's shape:
 * same `typeof`, an array for an array, and `null` counting as its own shape.
 * That is enough for strings, flags and numbers; anything with members --
 * an enum, a list of ids, a range -- should pass its own check.
 */
export function readSticky<T>(key: string, fallback: T, accept?: (raw: unknown) => raw is T): T | undefined {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    return undefined;
  }
  if (raw === null) return undefined;
  let found: unknown;
  try {
    found = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (accept) return accept(found) ? found : undefined;
  return sameShape(found, fallback) ? (found as T) : undefined;
}

function sameShape(found: unknown, fallback: unknown): boolean {
  if (fallback === null) return found === null;
  if (Array.isArray(fallback)) return Array.isArray(found);
  return found !== null && !Array.isArray(found) && typeof found === typeof fallback;
}

export function writeSticky(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* a private window; the setting still holds for this page's life */
  }
}

/**
 * `useState`, remembered.
 *
 * Switching household does not remount a screen, so the key can change under
 * a live component. When it does, the value is read afresh from the new key
 * during render -- one frame of the other household's filters would ask the
 * server for account ids it has never heard of.
 */
export function useSticky<T>(
  screen: string,
  household: string,
  name: string,
  fallback: T,
  accept?: (raw: unknown) => raw is T,
): [T, (next: T | ((previous: T) => T)) => void] {
  const key = stickyKey(screen, household, name);
  const [held, setHeld] = useState(() => ({ key, value: readSticky(key, fallback, accept) ?? fallback }));
  let current = held;
  if (held.key !== key) {
    current = { key, value: readSticky(key, fallback, accept) ?? fallback };
    setHeld(current);
  }
  const set = useCallback(
    (next: T | ((previous: T) => T)) => {
      setHeld((previous) => {
        const base = previous.key === key ? previous.value : (readSticky(key, fallback, accept) ?? fallback);
        const resolved = typeof next === "function" ? (next as (previous: T) => T)(base) : next;
        writeSticky(key, resolved);
        return { key, value: resolved };
      });
    },
    // `fallback` and `accept` are read only when the key changes; a caller
    // passing a fresh literal each render must not make the setter unstable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  );
  return [current.value, set];
}

/**
 * The screens whose remembered settings say what somebody was looking for --
 * the register's free-text search and amount lookup, the reimbursements
 * report's accounts and dates. Keyed by household, not by person, so two
 * members of one household on one browser saw each other's last search
 * (#198). The appearance, the menu and the column widths are about the device
 * and stay.
 */
const PERSONAL = /^spendtracker\.(register|reimbursements)\.[^.]+\.[^.]+/;

/** Whose settings the personal keys above currently are. */
export const STICKY_OWNER_KEY = "spendtracker.shell.stickyOwner";

/** Forget every personal setting on this browser. */
export function forgetScreens(): void {
  try {
    const store = window.localStorage;
    const doomed: string[] = [];
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i);
      if (key && PERSONAL.test(key)) doomed.push(key);
    }
    for (const key of doomed) store.removeItem(key);
    store.removeItem(STICKY_OWNER_KEY);
  } catch {
    /* a private window holds nothing to forget */
  }
}

/**
 * Somebody has signed in. If the personal settings on this browser were left
 * by someone else -- a session that expired rather than signed out, then a
 * second person at the same keyboard -- they go before the screens read them.
 */
export function claimScreens(userId: string): void {
  try {
    const owner = window.localStorage.getItem(STICKY_OWNER_KEY);
    if (owner !== null && owner !== userId) forgetScreens();
    window.localStorage.setItem(STICKY_OWNER_KEY, userId);
  } catch {
    /* as above */
  }
}
