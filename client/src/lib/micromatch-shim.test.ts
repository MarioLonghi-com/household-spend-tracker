/**
 * The micromatch @lingui/cli gets is `client/vendor/micromatch` (#270): its
 * two functions on picomatch alone, so `braces` (GHSA-vfj7-8cjw-p6xm) is not
 * installed. These are the answers real micromatch 4.0.8 gave for the calls
 * Lingui makes -- `capture` finds a catalog's locale from its path when the
 * Vite plugin loads a `.po`, `any` skips ignored files in watch mode.
 */
import { describe, expect, it } from "vitest";
// @ts-expect-error -- a CommonJS stand-in with no types of its own.
import micromatch from "micromatch";

describe("the micromatch stand-in", () => {
  it("is the one installed: nothing but the two calls, and no braces", () => {
    expect(Object.keys(micromatch).sort()).toEqual(["any", "capture", "default", "isMatch"]);
    expect(micromatch.braces).toBeUndefined();
  });

  it("captures the locale from a catalog path, as Lingui asks", () => {
    expect(micromatch.capture("src/locales/*/messages.po", "src/locales/pt-BR/messages.po")).toEqual(["pt-BR"]);
    expect(micromatch.capture("src/locales/*/messages.po", "src/locales/en-XA/messages.po")).toEqual(["en-XA"]);
    expect(micromatch.capture("src/locales/*/messages.po", "src/lib/i18n.ts")).toBeUndefined();
  });

  it("treats the braces Lingui escapes as plain text", () => {
    expect(micromatch.capture("src/\\{x\\}/*/messages.po", "src/{x}/sv-SE/messages.po")).toEqual(["sv-SE"]);
  });

  it("matches any of several patterns", () => {
    expect(micromatch.any("src/a.test.ts", ["**/*.test.ts", "**/*.test.tsx"])).toBe(true);
    expect(micromatch.any("src/a.ts", ["**/*.test.ts", "**/*.test.tsx"])).toBe(false);
  });
});
