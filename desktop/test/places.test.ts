import path from "node:path";
import { describe, expect, it } from "vitest";
import { DATA_FOLDER, placesIn, rootFor } from "../src/places";

describe("where the desktop app keeps things (#317)", () => {
  it("is %LOCALAPPDATA% on Windows, not Roaming and not the package's copy", () => {
    expect(rootFor({ platform: "win32", env: { LOCALAPPDATA: "C:\\Users\\a\\AppData\\Local", APPDATA: "C:\\Users\\a\\AppData\\Roaming" }, appData: "C:\\Users\\a\\AppData\\Roaming" })).toBe(
      "C:\\Users\\a\\AppData\\Local\\Spend Tracker",
    );
  });

  it("is Application Support on a Mac, which the sandbox puts in its container", () => {
    const appData = "/Volumes/Home/a/Library/Containers/app.spendtracker.spendtracker/Data/Library/Application Support";
    expect(rootFor({ platform: "darwin", env: {}, appData })).toBe(path.join(appData, "Spend Tracker"));
  });

  it("keeps the ledger's folder name whatever the app is called (#346)", () => {
    // Electron's userData follows the product name, now "Household Spend
    // Tracker": the ledger an earlier build made under "Spend Tracker" is
    // still the one opened, on both platforms.
    const appData = "/Volumes/Home/a/Library/Application Support";
    expect(DATA_FOLDER).toBe("Spend Tracker");
    // path.join, not a literal: the suite also runs on Windows, where it joins with "\\".
    expect(rootFor({ platform: "darwin", env: {}, appData })).toBe(path.join(appData, "Spend Tracker"));
    expect(rootFor({ platform: "win32", env: { LOCALAPPDATA: "C:\\L" }, appData: "C:\\R" })).toBe("C:\\L\\Spend Tracker");
  });

  it("can be moved for the tests", () => {
    expect(rootFor({ platform: "darwin", env: { SPENDTRACKER_DESKTOP_ROOT: "/tmp/st" }, appData: "/x" })).toBe(path.resolve("/tmp/st"));
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
