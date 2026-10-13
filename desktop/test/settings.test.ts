import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { DEFAULTS, load, parse, save } from "../src/settings";

describe("the shell's settings (#62)", () => {
  it("round-trips through the file", () => {
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "st-settings-")), "shell.json");
    const chosen = { ...DEFAULTS, backupsFolder: "/Users/someone/Dropbox/Spend", lastDailyBackup: "2026-10-13", backedUpTo: 42, language: "en-XA" };
    save(file, chosen);
    expect(load(file)).toEqual(chosen);
    expect(fs.existsSync(`${file}.writing`)).toBe(false);
  });

  it("falls back field by field, never refusing to start", () => {
    expect(parse({ backupsFolder: 7, backedUpTo: "lots", lastDailyBackup: "yesterday", language: "", askedForFolder: true })).toEqual({
      ...DEFAULTS,
      askedForFolder: true,
    });
    expect(parse("not an object")).toEqual(DEFAULTS);
  });

  it("a missing or broken file is the defaults", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "st-settings-"));
    expect(load(path.join(dir, "absent.json"))).toEqual(DEFAULTS);
    fs.writeFileSync(path.join(dir, "broken.json"), "{ half");
    expect(load(path.join(dir, "broken.json"))).toEqual(DEFAULTS);
  });
});
