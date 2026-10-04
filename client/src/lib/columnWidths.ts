/**
 * Column widths somebody chose by dragging a heading's edge (#74).
 *
 * ## Until you drag, the browser decides
 *
 * A register nobody has resized is laid out exactly as before: `table-layout:
 * auto`, each column as wide as what is in it, the caps in `register.css`
 * keeping a long payee from taking the row. The first drag is what changes
 * that. It measures every heading *as the browser drew it*, stores all of
 * them, and from then on the table is `table-layout: fixed` with one `<col>`
 * per column -- so the columns you did not touch stay where they were rather
 * than jumping to some default the moment one of their neighbours moved.
 *
 * ## A row is still one line
 *
 * Fixed layout is what makes that a promise rather than a hope. Under `auto`,
 * a column narrower than its content is a request the browser may ignore;
 * under `fixed` it is the width, and every cell truncates to it with an
 * ellipsis. The full text of every truncated cell was already on hover and in
 * its accessible name.
 *
 * ## Per device, not per account
 *
 * Stored in `localStorage`, like the appearance: how wide a payee column
 * should be depends on the screen it is drawn on, and the laptop and the big
 * monitor deserve different answers. Every read and write is in a `try` -- a
 * private window throws, and the register has to draw anyway.
 *
 * ## Keyed by meaning, not position
 *
 * A key is `payee` or `out-EUR`, never "the fourth column". Turning a currency
 * off removes two columns from the middle of the row; a width stored by index
 * would then belong to whatever slid into its place.
 */

import { useCallback, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";

export const COLUMN_WIDTHS_KEY = "spendtracker.register.columns";

/**
 * Narrow enough to take a column down to a letter and its arrow, wide enough
 * that the edge you would drag it back out by is still there to grab.
 */
export const MIN_WIDTH = 36;
/** A payee column wider than most windows is a slip of the mouse, not a choice. */
export const MAX_WIDTH = 900;
/** What one arrow key moves an edge by. Shift moves it five times as far. */
export const KEY_STEP = 10;

export type Widths = Record<string, number>;

export function clampWidth(value: number): number {
  return Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, value)));
}

/**
 * What is stored, as widths this version can use.
 *
 * Anything that is not an object of finite numbers is dropped rather than
 * repaired, one entry at a time: a hand-edited store should cost the column
 * it broke, not every width on the screen.
 */
export function parseWidths(raw: string | null): Widths {
  if (!raw) return {};
  let found: unknown;
  try {
    found = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!found || typeof found !== "object" || Array.isArray(found)) return {};
  const widths: Widths = {};
  for (const [key, value] of Object.entries(found as Record<string, unknown>)) {
    if (typeof value === "number" && Number.isFinite(value)) widths[key] = clampWidth(value);
  }
  return widths;
}

export function storedWidths(storageKey: string = COLUMN_WIDTHS_KEY): Widths {
  try {
    return parseWidths(window.localStorage.getItem(storageKey));
  } catch {
    return {};
  }
}

function remember(widths: Widths, storageKey: string): void {
  try {
    // Nothing chosen removes the key rather than storing `{}`, so "never
    // resized" and "reset" are one state and not two.
    if (Object.keys(widths).length === 0) window.localStorage.removeItem(storageKey);
    else window.localStorage.setItem(storageKey, JSON.stringify(widths));
  } catch {
    /* a private window; the widths still apply for this page's life */
  }
}

/**
 * The widths with every current column filled in.
 *
 * `measured` is what the browser drew, heading by heading; it is used for
 * any column with no width of its own yet. Stored widths win -- they are
 * what is on screen already, and the measurement of a fixed-layout table
 * would only read them back.
 */
export function seedWidths(keys: string[], widths: Widths, measured: number[]): Widths {
  const next: Widths = { ...widths };
  keys.forEach((key, at) => {
    if (next[key] === undefined && measured[at] !== undefined) next[key] = clampWidth(measured[at]);
  });
  return next;
}

/**
 * How wide the table is: exactly the sum of its columns.
 *
 * Not "at least the card". Under fixed layout a table wider than its columns
 * shares the difference out among all of them, so the edge being dragged
 * would stop following the pointer. A table narrower than the card is what
 * narrowing its columns means; one wider scrolls sideways inside it.
 */
export function tableWidth(keys: string[], widths: Widths, fallback: (key: string) => number): number {
  return keys.reduce((sum, key) => sum + (widths[key] ?? fallback(key)), 0);
}

export function useColumnWidths(
  keys: string[],
  fallback: (key: string) => number,
  storageKey: string = COLUMN_WIDTHS_KEY,
) {
  const [widths, setWidths] = useState<Widths>(() => storedWidths(storageKey));
  // The drag reads the latest widths from here rather than from a closure,
  // which would be the widths as they were when the handle last rendered.
  const latest = useRef(widths);
  latest.current = widths;

  const sized = Object.keys(widths).length > 0;
  const widthOf = useCallback((key: string) => widths[key] ?? fallback(key), [widths, fallback]);

  const commit = useCallback(
    (next: Widths) => {
      latest.current = next;
      setWidths(next);
      remember(next, storageKey);
    },
    [storageKey],
  );

  /** Every heading in `row`, measured, merged into what is stored. */
  const seeded = useCallback(
    (row: HTMLTableRowElement): Widths => {
      const measured = Array.from(row.children, (cell) => cell.getBoundingClientRect().width);
      return seedWidths(keys, latest.current, measured);
    },
    [keys],
  );

  /**
   * Drag an edge.
   *
   * While the pointer moves, the width is written straight onto the `<col>`
   * and the table rather than through React: a state change per mousemove
   * re-renders every row of the register, and that is hundreds of rows sixty
   * times a second. The state is set once, when the pointer lets go.
   */
  const startDrag = useCallback(
    (key: string, event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const handle = event.currentTarget;
      const row = handle.closest("tr");
      const table = handle.closest("table");
      if (!row || !table) return;

      const start = seeded(row as HTMLTableRowElement);
      // Rendered synchronously: React flushes a discrete event's updates
      // before the next one, so the `<col>`s exist by the first move.
      commit(start);
      const origin = event.clientX;
      const from = start[key];
      let width = from;

      handle.setPointerCapture?.(event.pointerId);
      handle.classList.add("dragging");
      document.body.classList.add("resizing-columns");

      const others = tableWidth(
        keys.filter((one) => one !== key),
        start,
        fallback,
      );
      const move = (next: PointerEvent) => {
        width = clampWidth(from + next.clientX - origin);
        const col = table.querySelector<HTMLTableColElement>(`col[data-col="${key}"]`);
        if (col) col.style.width = `${width}px`;
        table.style.setProperty("--table-width", `${others + width}px`);
      };
      const end = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", end);
        handle.removeEventListener("pointercancel", end);
        handle.classList.remove("dragging");
        document.body.classList.remove("resizing-columns");
        commit({ ...latest.current, [key]: width });
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", end);
      handle.addEventListener("pointercancel", end);
    },
    [keys, fallback, seeded, commit],
  );

  /** Arrow keys move the edge; Enter puts every column back. */
  const onKey = useCallback(
    (key: string, event: ReactKeyboardEvent<HTMLElement>) => {
      const step = event.shiftKey ? KEY_STEP * 5 : KEY_STEP;
      const delta = event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0;
      if (event.key === "Enter") {
        event.preventDefault();
        commit({});
        return;
      }
      if (!delta) return;
      event.preventDefault();
      const row = event.currentTarget.closest("tr");
      if (!row) return;
      const start = seeded(row as HTMLTableRowElement);
      commit({ ...start, [key]: clampWidth(start[key] + delta) });
    },
    [seeded, commit],
  );

  const reset = useCallback(() => commit({}), [commit]);

  return { widths, sized, widthOf, startDrag, onKey, reset };
}
