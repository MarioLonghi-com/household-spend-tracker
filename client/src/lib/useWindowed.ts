/**
 * Showing a long list without waiting for it.
 *
 * The measurement that decided this, on the machine this is developed on:
 *
 *   rows    fetch + serialise    put in the DOM
 *   250               ~5 ms              105 ms
 *   1,000             12 ms              423 ms
 *   5,000             60 ms            2,008 ms
 *   20,000           228 ms            7,766 ms
 *
 * Getting the data is cheap and painting it is not, by more than an order of
 * magnitude. So the split is: **fetch everything, render a window.** The whole
 * filtered set arrives in one request — no pages, no "load more", searching and
 * sorting stay server-side and exact — and the browser is handed a few hundred
 * rows at a time as you scroll toward them.
 *
 * Which means "it all loads at once" is true in the sense that matters: every
 * row is in memory the moment the request returns. What is incremental is only
 * the painting, and the only way to notice is to scroll faster than a screen
 * per frame.
 *
 * Extending is deliberately not tied to a scroll handler. A sentinel element
 * below the last row and an IntersectionObserver cost nothing while you are
 * still reading, where a scroll listener runs on every frame of every scroll.
 *
 * **The sentinel is a button, and that is not decoration.** An
 * IntersectionObserver is suspended while a tab is hidden, and there are other
 * ways for it not to fire; if extending depended on it alone, the failure would
 * be a list stuck part-way with nothing to do about it -- which is the same
 * shape as the truncation this replaced. So the observer is the convenience and
 * the button is the guarantee. `extend` is exported for it.
 *
 * **When the rows in hand are not all there is** (#100: the register asks the
 * server for five hundred at a time), `more` is how the list asks for the
 * next lot. Reaching the end of the rows in hand -- the sentinel, or the
 * button -- calls it, and the window is already a step longer when the rows
 * arrive, so they are drawn without a second trip to the sentinel. Pass it
 * only while a further page exists and none is on its way: called twice, a
 * page would be asked for twice.
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** Rows added per step. 250 measured at ~105ms, which is one dropped frame. */
export const STEP = 250;

/**
 * `resetKey`, when given, is what "a new list" means instead of the array's
 * identity. The Import preview needs it: categorising one line replaces the
 * lines array, and resetting on that would roll the window back to the top and
 * unmount the row somebody is working on 600 lines down. There the question
 * only changes with the batch, the sort or the direction.
 */
export function useWindowed<T>(
  rows: T[],
  step: number = STEP,
  resetKey?: unknown,
  more?: () => void,
) {
  const [shown, setShown] = useState(step);
  const sentinel = useRef<HTMLButtonElement | null>(null);

  // A new list is a new question — a different filter, account or sort — so it
  // starts from the top rather than keeping however far the last one was
  // unrolled. `rows.length` alone would not notice a filter that happens to
  // match the same number of rows, so the identity of the array is the key.
  const question = resetKey === undefined ? rows : resetKey;
  useEffect(() => {
    setShown(step);
  }, [question, step]);

  const attach = useCallback((node: HTMLButtonElement | null) => {
    sentinel.current = node;
  }, []);

  useEffect(() => {
    const node = sentinel.current;
    if (!node || (shown >= rows.length && !more)) return;

    const watcher = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        if (shown < rows.length) {
          setShown((was) => Math.min(was + step, rows.length));
        } else if (more) {
          setShown((was) => was + step);
          more();
        }
      },
      // Start the next batch before the sentinel is actually on screen, so the
      // rows are there by the time you reach where they go.
      { rootMargin: "600px" },
    );
    watcher.observe(node);
    return () => watcher.disconnect();
  }, [shown, rows.length, step, more]);

  const extend = useCallback(() => {
    if (shown >= rows.length && more) {
      setShown((was) => was + step);
      more();
      return;
    }
    setShown((was) => Math.min(was + step, rows.length));
  }, [shown, step, rows.length, more]);

  return {
    visible: shown >= rows.length ? rows : rows.slice(0, shown),
    /** Put this on the control after the last row. */
    sentinelRef: attach,
    /** What that control does when pressed, and the observer's fallback. */
    extend,
    allShown: shown >= rows.length,
    shown: Math.min(shown, rows.length),
    total: rows.length,
  };
}
