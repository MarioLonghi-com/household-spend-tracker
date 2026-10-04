/**
 * `format()` runs per row on every screen, so it must never throw (#193).
 */

import { describe, expect, it } from "vitest";
import { format } from "./money";

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
