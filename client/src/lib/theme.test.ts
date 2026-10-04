// @vitest-environment jsdom

/**
 * What `applyColours` will paste into its `<style>`.
 *
 * This sheet is the reason the CSP keeps `style-src 'unsafe-inline'`, so a
 * value that closes the rule and opens another is a stylesheet written by
 * whoever controls the value. The server validates colours before storing
 * them; these assert the client does not rely on that alone (#108). Every
 * assertion is on the text that reached the sheet.
 */

import { afterEach, describe, expect, it } from "vitest";
import { applyColours, safeEntries } from "./theme";
import type { Scheme } from "./types";

const LIGHT: Scheme = {
  ink: "#1a1a1a",
  muted: "#666666",
  paper: "#ffffff",
  surface: "#f6f6f4",
  surface_2: "#eeeeea",
  line: "#dddddd",
  accent: "#15705c",
  accent_ink: "#ffffff",
  danger: "#b3261e",
  warn: "#8a5a00",
  positive: "#1b6e3a",
};
const DARK: Scheme = { ...LIGHT, ink: "#f0f0f0", paper: "#121212", accent: "#5fc2a8" };

const sheet = () => document.getElementById("household-colours")?.textContent ?? "";

afterEach(() => applyColours(null));

describe("applyColours", () => {
  it("writes every well-formed colour, both schemes", () => {
    applyColours({ light: LIGHT, dark: DARK });

    expect(sheet()).toContain("--accent: #15705c;");
    expect(sheet()).toContain("--accent: #5fc2a8;");
    expect(sheet()).toContain("--surface-2: #eeeeea;");
  });

  it("leaves out a value that would close the rule and open another", () => {
    const hostile = { ...LIGHT, accent: "red; } body { background: url(//evil.test/x)" };
    applyColours({ light: hostile, dark: DARK });

    expect(sheet()).not.toContain("evil.test");
    expect(sheet()).not.toContain("body {");
    // The rest of the scheme still arrives; only the bad value is dropped.
    expect(sheet()).toContain("--ink: #1a1a1a;");
  });

  it("leaves out a variable name that is not a plain identifier", () => {
    const hostile = { ...LIGHT, ["x: red; } * { color"]: "#000000" } as unknown as Scheme;
    applyColours({ light: hostile, dark: DARK });

    expect(sheet()).not.toContain("* {");
    expect(sheet()).toContain("--paper: #ffffff;");
  });

  it("accepts #rrggbb and nothing looser", () => {
    const loose = {
      ...LIGHT,
      ink: "#fff",
      muted: "rgb(0,0,0)",
      paper: "#ffffff ",
      line: "#12345g",
      surface: 0x123456,
    } as unknown as Scheme;

    expect(safeEntries(loose).map(([name]) => name)).toEqual([
      "surface_2",
      "accent",
      "accent_ink",
      "danger",
      "warn",
      "positive",
    ]);
  });
});
