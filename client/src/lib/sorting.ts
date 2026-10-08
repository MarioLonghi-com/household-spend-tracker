/**
 * Ordering rows for a table.
 *
 * Its own file, with no React in it, because it is the one piece of this
 * screen furniture with a contract worth testing on its own: blanks last in
 * *both* directions, numbers as numbers, and a key that can be a tuple so two
 * currencies never interleave in a money column -- with the currency groups
 * held in one order whichever way the column points (#129).
 */

import { collator } from "./locale";

export type SortDirection = "asc" | "desc";

/** One cell's worth of sortable value. An array sorts on the first, then the next. */
export type SortValue = string | number | boolean | null | undefined;

/**
 * A key element the direction does not apply to.
 *
 * Put one ahead of the figure in a tuple key and it groups the rows in the
 * same order under both directions; only the elements after it turn round.
 * Build it with `fixed()`, never by hand, so the marker stays one shape.
 */
export interface FixedGroup {
  readonly fixed: SortValue;
}

/** Wrap a key element so reversing the column leaves its order alone. */
export function fixed(value: SortValue): FixedGroup {
  return { fixed: value };
}

/** What one element of a tuple key can be. */
export type SortKeyPart = SortValue | FixedGroup;

function isFixed(value: SortKeyPart): value is FixedGroup {
  return typeof value === "object" && value !== null && "fixed" in value;
}

/**
 * The key for a money column: currency groups first, then the figure.
 *
 * Minor units of EUR and GBP are not comparable quantities, so the currencies
 * never interleave. The groups keep one order in both directions -- the
 * household's base currency first, then the others A to Z -- and only the
 * figure inside each group turns round. Flipping the groups with the figure
 * made a descending column read "EUR 5, EUR 20, EUR 70" under "GBP 90", which
 * does not look sorted at all (#129).
 */
export function moneyKey(currency: string, amount: number, baseCurrency: string): SortKeyPart[] {
  return [fixed(currency === baseCurrency ? 0 : 1), fixed(currency), amount];
}

function one(a: SortValue, b: SortValue, sign: number): number {
  // Blank sorts last whichever way the column points: "unknown" is not a value,
  // and a column of empties at the top says nothing about the rows below it.
  // The caller is about to multiply by `sign`, so this undoes it -- otherwise
  // reversing the column brings every blank to the top, which is exactly the
  // rows with nothing to say pushed in front of the ones with something.
  const aEmpty = a === null || a === undefined || a === "";
  const bEmpty = b === null || b === undefined || b === "";
  if (aEmpty || bEmpty) return aEmpty && bEmpty ? 0 : (aEmpty ? 1 : -1) * sign;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return collator({ sensitivity: "base" }).compare(String(a), String(b));
}

/**
 * Sort rows by whatever `valueOf` says the sorted column holds.
 *
 * `valueOf` returning `null` for a column means "leave the rows alone" -- which
 * is how an unsorted default key passes through untouched. Returning an array
 * sorts on each in turn, ahead of the direction being applied, which is what
 * keeps two currencies from interleaving in a money column. An element wrapped
 * in `fixed()` sorts ascending whichever way the column points.
 */
export function sortRows<T, K extends string>(
  rows: T[],
  sort: K,
  direction: SortDirection,
  valueOf: (row: T, column: K) => SortValue | SortKeyPart[],
  tiebreak?: (a: T, b: T) => number,
): T[] {
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((rowA, rowB) => {
    const a = valueOf(rowA, sort);
    const b = valueOf(rowB, sort);
    if (Array.isArray(a) && Array.isArray(b)) {
      for (let at = 0; at < Math.max(a.length, b.length); at += 1) {
        const x = a[at];
        const y = b[at];
        if (isFixed(x) || isFixed(y)) {
          const by = one(isFixed(x) ? x.fixed : x, isFixed(y) ? y.fixed : y, 1);
          if (by !== 0) return by;
          continue;
        }
        const by = one(x, y, sign);
        if (by !== 0) return by * sign;
      }
    } else {
      const by = one(a as SortValue, b as SortValue, sign);
      if (by !== 0) return by * sign;
    }
    // A stable tiebreak, so equal cells do not shuffle between renders.
    return tiebreak ? tiebreak(rowA, rowB) : 0;
  });
}
