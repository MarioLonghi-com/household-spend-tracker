/**
 * `format()` runs per row on every screen, so it must never throw (#193).
 * `parse()` must read a typed thousand as a thousand (#45).
 */

import { describe, expect, it } from "vitest";
import { amountLookup, format, parse, toInput } from "./money";

describe("format", () => {
  it("renders a code Intl refuses as plain text instead of throwing", () => {
    expect(format(-5, "€€€")).toBe("-0.05 €€€");
    expect(() => format(1234, "12A")).not.toThrow();
    expect(format(1234, "12A")).toContain("12A");
    expect(format(1234, "12A")).toContain("12");
  });

  it("still formats real codes through Intl, with their own exponent", () => {
    expect(() => format(150, "JPY")).not.toThrow();
    expect(format(150, "JPY")).toContain("150");
    expect(format(150, "JPY")).not.toContain("1.50");
    expect(format(-12345, "EUR")).toContain("123.45");
    expect(format(-12345, "gbp")).toContain("£");
  });
});

// #45: "1,234" was read by position alone -- the last separator is the decimal
// mark -- so a typed thousand was stored as 1.23. A lone separator before
// exactly three digits now means what it means in the reader's number format.
describe("parse", () => {
  it.each([
    // text, currency, locale, minor units
    ["1,234", "EUR", "en-US", 123400],
    ["1,000", "EUR", "en-US", 100000],
    ["1.234", "EUR", "en-US", 123],
    ["1.234", "EUR", "de-DE", 123400],
    ["1,234", "EUR", "de-DE", 123],
    ["1.234", "BRL", "pt-BR", 123400],
    ["1,000", "BRL", "pt-BR", 100],
    ["1,234.56", "EUR", "en-US", 123456],
    ["1,234.56", "EUR", "de-DE", 123456],
    ["1.234,56", "EUR", "en-US", 123456],
    ["1.234,56", "EUR", "de-DE", 123456],
    ["12,34", "EUR", "en-US", 1234],
    ["12,34", "EUR", "de-DE", 1234],
    ["1,234,567", "EUR", "en-US", 123456700],
    ["1.234.567", "EUR", "de-DE", 123456700],
    ["-1,234", "EUR", "en-US", -123400],
    ["(1,234)", "EUR", "en-US", -123400],
    ["1234.567", "EUR", "de-DE", 123457],
    ["1,234", "JPY", "en-US", 1234],
    ["1.234", "JPY", "de-DE", 1234],
    ["1,234", "JPY", "de-DE", 1],
    ["1,234", "BHD", "de-DE", 1234],
    ["1.234", "BHD", "en-US", 1234],
    ["1,234", "BHD", "en-US", 1234000],
    [",234", "EUR", "en-US", 23],
  ] as const)("reads %s in %s under %s as %i", (text, currency, locale, minor) => {
    expect(parse(text, currency, locale)).toBe(minor);
  });

  it.each([
    // groups after the first are three digits, or it is not a number
    ["1,23,4", "en-US"],
    ["1.2.3", "de-DE"],
    ["1234,567,890", "en-US"],
    // de-CH groups with an apostrophe: a comma before three digits is neither
    ["1,234", "de-CH"],
  ])("refuses %s under %s rather than guess", (text, locale) => {
    expect(parse(text, "EUR", locale)).toBeNull();
  });

  it("reads back what toInput wrote, in every exponent and both decimal marks", () => {
    const cases: [number, string][] = [
      [1234, "BHD"], [1234567, "BHD"], [-1234, "BHD"],
      [123456, "EUR"], [5, "EUR"], [1234, "JPY"], [1234567, "JPY"],
    ];
    for (const locale of ["en-US", "de-DE", "pt-BR"]) {
      for (const [minor, currency] of cases) {
        const shown = toInput(minor, currency, locale);
        expect(parse(minor < 0 ? `-${shown}` : shown, currency, locale)).toBe(minor);
      }
    }
    expect(toInput(1234, "BHD", "de-DE")).toBe("1,234");
    expect(toInput(1234, "BHD", "en-US")).toBe("1.234");
  });
});

describe("amountLookup", () => {
  it("reads a typed thousand the way parse does", () => {
    expect(amountLookup("1,234", "en-US")).toBe("1234");
    expect(amountLookup("1,234", "de-DE")).toBe("1.234");
    expect(amountLookup("1.234", "de-DE")).toBe("1234");
    expect(amountLookup("1,234", "de-CH")).toBeNull();
  });
});
