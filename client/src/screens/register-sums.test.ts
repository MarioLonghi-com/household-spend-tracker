import { describe, expect, it } from "vitest";
import { selectionSums } from "./Register";

/**
 * What a run of selected rows comes to.
 *
 * The one piece of the register that is arithmetic rather than layout, and the
 * arithmetic is over money in more than one currency -- which is exactly where
 * the previous build went wrong three times. So this asserts the figures, and
 * asserts the *shape* of the answer too: two currencies give two totals and
 * there is no third number anywhere in it. A grand total across currencies
 * would need an exchange rate, and there is none in this schema.
 */

type TestRow = { id: string; amount: number; currency: string };

const rows: TestRow[] = [
  { id: "a", amount: -4_250, currency: "EUR" },
  { id: "b", amount: 120_000, currency: "EUR" },
  { id: "c", amount: -1_000, currency: "EUR" },
  { id: "d", amount: -8_000, currency: "GBP" },
  { id: "e", amount: 5_000, currency: "GBP" },
];

const currencyOf = (row: TestRow) => row.currency;

describe("the selection sum", () => {
  it("adds the ins and the outs of one currency, and nets them", () => {
    const [eur] = selectionSums(rows, new Set(["a", "b", "c"]), currencyOf, ["EUR", "GBP"]);

    expect(eur.currency).toBe("EUR");
    expect(eur.ins).toBe(120_000);
    expect(eur.outs).toBe(-5_250);
    expect(eur.net).toBe(114_750);
    expect(eur.count).toBe(3);
    // The line on screen reads "ins + outs = net", so it has to be true.
    expect(eur.ins + eur.outs).toBe(eur.net);
  });

  it("keeps two currencies apart and never adds them", () => {
    const sums = selectionSums(rows, new Set(["a", "b", "d", "e"]), currencyOf, [
      "EUR",
      "GBP",
    ]);

    expect(sums.map((one) => one.currency)).toEqual(["EUR", "GBP"]);
    expect(sums.map((one) => one.net)).toEqual([115_750, -3_000]);
    expect(sums.map((one) => one.count)).toEqual([2, 2]);

    // The point of the whole design: 115_750 + -3_000 = 112_750 is a number
    // this ledger cannot produce, so nothing in the answer may equal it.
    expect(sums.some((one) => one.net === 112_750)).toBe(false);
    expect(sums).toHaveLength(2);
  });

  it("ignores rows nobody selected", () => {
    const sums = selectionSums(rows, new Set(["d"]), currencyOf, ["EUR", "GBP"]);

    expect(sums).toHaveLength(1);
    expect(sums[0]).toMatchObject({ currency: "GBP", ins: 0, outs: -8_000, net: -8_000 });
  });

  it("gives nothing at all for an empty selection", () => {
    expect(selectionSums(rows, new Set(), currencyOf, ["EUR"])).toEqual([]);
  });

  it("orders the lines the way the currency toggle orders the columns", () => {
    const sums = selectionSums(rows, new Set(["a", "d"]), currencyOf, ["GBP", "EUR"]);

    expect(sums.map((one) => one.currency)).toEqual(["GBP", "EUR"]);
  });

  it("still counts a currency the toggle is not showing", () => {
    // A row can be selected and then hidden by the currency toggle. It is
    // still selected, and a bulk edit still reaches it, so the figures must
    // not quietly stop describing it.
    const sums = selectionSums(rows, new Set(["a", "d"]), currencyOf, ["EUR"]);

    expect(sums.map((one) => one.currency)).toEqual(["EUR", "GBP"]);
    expect(sums.map((one) => one.count)).toEqual([1, 1]);
  });

  it("counts a zero-amount row without inventing an inflow", () => {
    const withZero = [...rows, { id: "z", amount: 0, currency: "EUR" }];
    const [eur] = selectionSums(withZero, new Set(["z"]), currencyOf, ["EUR"]);

    expect(eur).toMatchObject({ ins: 0, outs: 0, net: 0, count: 1 });
  });
});
