import path from "node:path";
import { describe, expect, it } from "vitest";
import { placesIn, rootFor } from "../src/places";

describe("where the desktop app keeps things (#317)", () => {
  it("is %LOCALAPPDATA% on Windows, not Roaming and not the package's copy", () => {
    expect(rootFor({ platform: "win32", env: { LOCALAPPDATA: "C:\\Users\\a\\AppData\\Local", APPDATA: "C:\\Users\\a\\AppData\\Roaming" }, userData: "C:\\Users\\a\\AppData\\Roaming\\Spend Tracker" })).toBe(
      "C:\\Users\\a\\AppData\\Local\\Spend Tracker",
    );
  });

  it("is Application Support on a Mac, which the sandbox puts in its container", () => {
    const userData = "/Volumes/Home/a/Library/Containers/app.spendtracker.spendtracker/Data/Library/Application Support/Spend Tracker";
    expect(rootFor({ platform: "darwin", env: {}, userData })).toBe(userData);
  });

  it("can be moved for the tests", () => {
    expect(rootFor({ platform: "darwin", env: { SPENDTRACKER_DESKTOP_ROOT: "/tmp/st" }, userData: "/x" })).toBe(path.resolve("/tmp/st"));
  });

  it("puts the ledger, the settings and the log side by side under it", () => {
    const places = placesIn("/root");
    expect(places).toEqual({
      root: "/root",
      ledger: path.join("/root", "Ledger"),
      settings: path.join("/root", "shell.json"),
      log: path.join("/root", "shell.log"),
      incoming: path.join("/root", "Ledger", ".incoming"),
    });
  });
});
