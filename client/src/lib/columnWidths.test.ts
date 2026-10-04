// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";
import {
  COLUMN_WIDTHS_KEY,
  MAX_WIDTH,
  MIN_WIDTH,
  clampWidth,
  parseWidths,
  seedWidths,
  storedWidths,
  tableWidth,
} from "./columnWidths";

beforeEach(() => {
  window.localStorage.clear();
});

describe("clampWidth", () => {
  it("keeps an edge you can still grab, and rounds to a pixel", () => {
    expect(clampWidth(3)).toBe(MIN_WIDTH);
    expect(clampWidth(5000)).toBe(MAX_WIDTH);
    expect(clampWidth(120.6)).toBe(121);
  });
});

describe("parseWidths", () => {
  it("drops the entry a hand edit broke, not the ones beside it", () => {
    const raw = JSON.stringify({ payee: 240, memo: "wide", date: null, account: 2 });
    expect(parseWidths(raw)).toEqual({ payee: 240, account: MIN_WIDTH });
  });

  it("treats anything that is not an object of numbers as nothing chosen", () => {
    expect(parseWidths(null)).toEqual({});
    expect(parseWidths("not json")).toEqual({});
    expect(parseWidths("[100, 200]")).toEqual({});
    expect(parseWidths("42")).toEqual({});
  });

  it("reads back what the register stored", () => {
    window.localStorage.setItem(COLUMN_WIDTHS_KEY, JSON.stringify({ "out-EUR": 90, "out-GBP": 130 }));
    expect(storedWidths()).toEqual({ "out-EUR": 90, "out-GBP": 130 });
  });
});

describe("seedWidths", () => {
  const keys = ["select", "date", "payee", "out-EUR", "out-GBP"];

  it("measures every column nobody has chosen a width for", () => {
    expect(seedWidths(keys, {}, [28, 96.4, 310, 104, 104])).toEqual({
      select: MIN_WIDTH,
      date: 96,
      payee: 310,
      "out-EUR": 104,
      "out-GBP": 104,
    });
  });

  it("keeps a chosen width over the measurement, and keeps widths of columns not on screen", () => {
    // `in-USD` belongs to a currency the toggle has hidden. Its width must
    // still be there when the currency comes back.
    const chosen = { payee: 400, "in-USD": 150 };
    const next = seedWidths(keys, chosen, [28, 96, 180, 104, 104]);
    expect(next.payee).toBe(400);
    expect(next["in-USD"]).toBe(150);
    expect(next.date).toBe(96);
  });
});

describe("tableWidth", () => {
  it("is exactly the sum of its columns, two currencies side by side", () => {
    const keys = ["date", "out-EUR", "in-EUR", "out-GBP", "in-GBP"];
    const widths = { date: 100, "out-EUR": 90, "in-EUR": 90 };
    // The GBP pair has no width yet, so it takes the fallback.
    expect(tableWidth(keys, widths, () => 110)).toBe(100 + 90 + 90 + 110 + 110);
  });
});
