import { createRequire } from "node:module";
import path from "node:path";
import { describe, expect, it } from "vitest";

// Plain CommonJS, as electron-builder loads it: through Node's own `require`
// rather than the test runner's transform.
const require = createRequire(import.meta.url);
const fuses = require("../scripts/fuses.cjs") as {
  WANTED: Record<string, boolean>;
  mode: (env: Record<string, string | undefined>) => "on" | "off";
  expected: (which: "on" | "off") => Record<string, boolean>;
  binary: (appOutDir: string, platform: string, product: string) => string;
  wrong: (found: Record<string, boolean>, which: "on" | "off") => string[];
};

describe("the fuses (#63)", () => {
  it("closes the three ways in and locks the app to its asar", () => {
    expect(fuses.WANTED).toEqual({
      RunAsNode: false,
      EnableNodeOptionsEnvironmentVariable: false,
      EnableNodeCliInspectArguments: false,
      OnlyLoadAppFromAsar: true,
      EnableEmbeddedAsarIntegrityValidation: true,
    });
  });

  it("is off unless SPENDTRACKER_FUSES says on, and refuses anything else", () => {
    expect(fuses.mode({})).toBe("off");
    expect(fuses.mode({ SPENDTRACKER_FUSES: "" })).toBe("off");
    expect(fuses.mode({ SPENDTRACKER_FUSES: "off" })).toBe("off");
    expect(fuses.mode({ SPENDTRACKER_FUSES: " ON " })).toBe("on");
    expect(() => fuses.mode({ SPENDTRACKER_FUSES: "1" })).toThrow(/"1"/);
  });

  it("expects an unfused build to be exactly as Electron ships it", () => {
    const on = fuses.expected("on");
    const off = fuses.expected("off");
    for (const name of Object.keys(fuses.WANTED)) expect(off[name], name).toBe(!on[name]);
    expect(off.EnableNodeCliInspectArguments).toBe(true);
    expect(off.EnableEmbeddedAsarIntegrityValidation).toBe(false);
  });

  it("names every fuse that is not where the mode puts it", () => {
    const fused = fuses.expected("on");
    expect(fuses.wrong(fused, "on")).toEqual([]);
    expect(fuses.wrong(fused, "off")).toHaveLength(5);
    expect(fuses.wrong({ ...fused, RunAsNode: true }, "on")).toEqual(["RunAsNode: is on, should be off"]);
    expect(fuses.wrong({ ...fused, OnlyLoadAppFromAsar: false }, "on")).toEqual(["OnlyLoadAppFromAsar: is off, should be on"]);
  });

  it("finds the binary electron-builder left, by platform", () => {
    expect(fuses.binary("/out/mac-arm64", "darwin", "Household Spend Tracker")).toBe(path.join("/out/mac-arm64", "Household Spend Tracker.app"));
    expect(fuses.binary("/out/mas-arm64", "mas", "Household Spend Tracker")).toBe(path.join("/out/mas-arm64", "Household Spend Tracker.app"));
    expect(fuses.binary("C:\\out\\win-unpacked", "win32", "Household Spend Tracker")).toBe(path.join("C:\\out\\win-unpacked", "Household Spend Tracker.exe"));
    expect(() => fuses.binary("/out", "linux", "x")).toThrow(/linux/);
  });
});
