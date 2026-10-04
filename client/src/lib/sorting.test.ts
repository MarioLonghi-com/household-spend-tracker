/**
 * What `sortRows` promises every table on this screen.
 *
 * The blanks-in-both-directions case is here because it was wrong: the empty
 * check ran before the direction was applied, so reversing any column brought
 * every blank row to the top -- the rows with nothing to say pushed in front of
 * the ones with something.
 */

import { describe, expect, it } from "vitest";
import { fixed, moneyKey, sortRows } from "./sorting";

type Row = { n: string; v?: unknown };
const names = (rows: Row[]) => rows.map((row) => row.n);
const by = (row: Row) => row.v as never;

describe("sortRows", () => {
  it("puts blanks last whichever way the column points", () => {
    const rows: Row[] = [{ n: "b", v: "b" }, { n: "blank", v: null }, { n: "a", v: "a" }];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["a", "b", "blank"]);
    expect(names(sortRows(rows, "v", "desc", by))).toEqual(["b", "a", "blank"]);
  });

  it("treats an empty string as blank, not as the smallest string", () => {
    const rows: Row[] = [{ n: "named", v: "Santander" }, { n: "unnamed", v: "" }];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["named", "unnamed"]);
  });

  it("sorts numbers as numbers", () => {
    const rows: Row[] = [{ n: "ten", v: 10 }, { n: "two", v: 2 }, { n: "nine", v: 9 }];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["two", "nine", "ten"]);
  });

  it("puts money out below money in", () => {
    const rows: Row[] = [{ n: "in", v: 5_000 }, { n: "out", v: -12_000 }];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["out", "in"]);
  });

  it("never interleaves two currencies in a money column", () => {
    // Minor units of EUR and GBP are not comparable quantities. The tuple key
    // is what keeps them apart while still ranking the figures inside each.
    const rows: Row[] = [
      { n: "gbp-1", v: ["GBP", 100] },
      { n: "eur-9", v: ["EUR", 900] },
      { n: "gbp-9", v: ["GBP", 900] },
      { n: "eur-1", v: ["EUR", 100] },
    ];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["eur-1", "eur-9", "gbp-1", "gbp-9"]);
    expect(names(sortRows(rows, "v", "desc", by))).toEqual(["gbp-9", "gbp-1", "eur-9", "eur-1"]);
  });

  it("keeps a fixed-group element in its order under desc, and flips what follows it", () => {
    const rows: Row[] = [
      { n: "b-1", v: [fixed("b"), 1] },
      { n: "a-2", v: [fixed("a"), 2] },
      { n: "b-2", v: [fixed("b"), 2] },
      { n: "a-1", v: [fixed("a"), 1] },
    ];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["a-1", "a-2", "b-1", "b-2"]);
    expect(names(sortRows(rows, "v", "desc", by))).toEqual(["a-2", "a-1", "b-2", "b-1"]);
  });

  it("puts the base currency's group first in a money column, then the rest A to Z, both ways", () => {
    // The five pairs from #129, plus two more currencies to show the A-to-Z.
    // Only the figures turn round; the groups stay where they are.
    const money: [string, string, number][] = [
      ["£15", "GBP", 1500],
      ["€70", "EUR", 7000],
      ["£90", "GBP", 9000],
      ["€5", "EUR", 500],
      ["€20", "EUR", 2000],
      ["$1", "USD", 100],
      ["CHF1", "CHF", 100],
    ];
    const rowsIn = (base: string): Row[] =>
      money.map(([n, currency, amount]) => ({ n, v: moneyKey(currency, amount, base) }));

    const eur = rowsIn("EUR");
    expect(names(sortRows(eur, "v", "asc", by))).toEqual(["€5", "€20", "€70", "CHF1", "£15", "£90", "$1"]);
    expect(names(sortRows(eur, "v", "desc", by))).toEqual(["€70", "€20", "€5", "CHF1", "£90", "£15", "$1"]);

    const gbp = rowsIn("GBP");
    expect(names(sortRows(gbp, "v", "asc", by))).toEqual(["£15", "£90", "CHF1", "€5", "€20", "€70", "$1"]);
    expect(names(sortRows(gbp, "v", "desc", by))).toEqual(["£90", "£15", "CHF1", "€70", "€20", "€5", "$1"]);
  });

  it("folds case and accents, so Ávila files under A", () => {
    const rows: Row[] = [
      { n: "z", v: "Zaragoza" },
      { n: "a", v: "Ávila" },
      { n: "b", v: "badajoz" },
    ];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["a", "b", "z"]);
  });

  it("sorts false before true", () => {
    const rows: Row[] = [{ n: "on", v: true }, { n: "off", v: false }];
    expect(names(sortRows(rows, "v", "asc", by))).toEqual(["off", "on"]);
  });

  it("leaves the rows exactly as they came when the key says nothing", () => {
    // This is how a list opens on an order the server chose, with no heading lit.
    const rows: Row[] = [{ n: "third" }, { n: "first" }, { n: "second" }];
    const kept = sortRows(rows, "order", "asc", () => null, () => 0);
    expect(names(kept)).toEqual(["third", "first", "second"]);
  });

  it("breaks ties the way the caller asked, not at random", () => {
    const rows: Row[] = [{ n: "b", v: 1 }, { n: "c", v: 1 }, { n: "a", v: 1 }];
    const sorted = sortRows(rows, "v", "asc", by, (x, y) => x.n.localeCompare(y.n));
    expect(names(sorted)).toEqual(["a", "b", "c"]);
  });

  it("does not disturb the array it was given", () => {
    const rows: Row[] = [{ n: "b", v: "b" }, { n: "a", v: "a" }];
    sortRows(rows, "v", "asc", by);
    expect(names(rows)).toEqual(["b", "a"]);
  });
});
