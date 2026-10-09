import { describe, expect, it } from "vitest";
import { seedLocale } from "./seedWords";

describe("which language a new household is seeded in (#268)", () => {
  it("sends nothing in English, so the request is what it always was", () => {
    expect(seedLocale("en", ["en"])).toEqual({});
    expect(seedLocale("en", ["en", "pt-BR"])).toEqual({});
  });

  it("sends nothing for a language that is not served, the pseudo-locale included", () => {
    expect(seedLocale("pt-BR", ["en"])).toEqual({});
    expect(seedLocale("en-XA", ["en"])).toEqual({});
  });

  it("sends a served language", () => {
    expect(seedLocale("pt-BR", ["en", "pt-BR"])).toEqual({ locale: "pt-BR" });
  });

  it("sends nothing today, whatever the device is on", () => {
    expect(seedLocale()).toEqual({});
  });
});
