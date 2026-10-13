import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { bringIn, defaultFolder, deliver, localDay } from "../src/backups";

function made(dir: string, name: string, content = "SQLite format 3\u0000 ledger") {
  const outgoing = path.join(dir, ".outgoing");
  fs.mkdirSync(outgoing, { recursive: true });
  fs.writeFileSync(path.join(outgoing, name), content);
  return { path: path.join(outgoing, name), name };
}

describe("backups the person can see (#62)", () => {
  it("moves the backend's copy into the folder whole, and prunes after", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "st-backups-"));
    const folder = path.join(dir, "Spend Tracker Backups");
    fs.mkdirSync(folder);
    fs.writeFileSync(path.join(folder, "spendtracker-20261012T080000Z.sqlite3"), "yesterday, earlier");
    fs.writeFileSync(path.join(folder, "spendtracker-20261012T200000Z.sqlite3"), "yesterday, newest");
    fs.writeFileSync(path.join(folder, "spendtracker-20261013T070000Z.sqlite3.partial"), "a crash left this");
    fs.writeFileSync(path.join(folder, "family photo.jpg"), "theirs");

    const copy = made(dir, "spendtracker-20261013T190000Z.sqlite3");
    const delivered = deliver(copy, folder);

    expect(delivered.file).toBe(path.join(folder, "spendtracker-20261013T190000Z.sqlite3"));
    expect(fs.readFileSync(delivered.file, "utf8")).toBe("SQLite format 3\u0000 ledger");
    expect(fs.existsSync(copy.path)).toBe(false);
    expect(delivered.removed).toEqual(["spendtracker-20261012T080000Z.sqlite3"]);
    expect(fs.readdirSync(folder).sort()).toEqual([
      "family photo.jpg",
      "spendtracker-20261012T200000Z.sqlite3",
      "spendtracker-20261013T190000Z.sqlite3",
    ]);
  });

  it("never overwrites a backup from the same second", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "st-backups-"));
    const folder = path.join(dir, "Backups");
    const first = deliver(made(dir, "spendtracker-20261013T190000Z.sqlite3", "first"), folder);
    const second = deliver(made(dir, "spendtracker-20261013T190000Z.sqlite3", "second"), folder);
    const third = deliver(made(dir, "spendtracker-20261013T190000Z.sqlite3", "third"), folder);
    expect(fs.readFileSync(first.file, "utf8")).toBe("first");
    expect(path.basename(second.file)).toBe("spendtracker-20261013T190000Z-2.sqlite3");
    expect(fs.readFileSync(second.file, "utf8")).toBe("second");
    expect(path.basename(third.file)).toBe("spendtracker-20261013T190000Z-3.sqlite3");
    expect(fs.readdirSync(folder).sort()).toEqual([
      "spendtracker-20261013T190000Z-2.sqlite3",
      "spendtracker-20261013T190000Z-3.sqlite3",
      "spendtracker-20261013T190000Z.sqlite3",
    ]);
  });

  it("makes the folder when it is not there yet", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "st-backups-"));
    const folder = path.join(dir, "new", "Backups");
    deliver(made(dir, "spendtracker-20261013T190000Z.sqlite3"), folder);
    expect(fs.readdirSync(folder)).toEqual(["spendtracker-20261013T190000Z.sqlite3"]);
  });

  it("defaults to Documents, or beside the ledger where the sandbox cannot reach Documents", () => {
    expect(defaultFolder("/Users/a/Documents", "/sandbox/Backups", false)).toBe(path.join("/Users/a/Documents", "Spend Tracker Backups"));
    expect(defaultFolder("/Users/a/Documents", "/sandbox/Backups", true)).toBe("/sandbox/Backups");
  });

  it("brings a chosen backup in, replacing whatever an earlier restore left", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "st-backups-"));
    const incoming = path.join(dir, "Ledger", ".incoming");
    fs.mkdirSync(incoming, { recursive: true });
    fs.writeFileSync(path.join(incoming, "stale.sqlite3"), "old");
    fs.writeFileSync(path.join(dir, "picked.zip"), "PK zip");
    const copy = bringIn(path.join(dir, "picked.zip"), incoming);
    expect(copy).toBe(path.join(incoming, "picked.zip"));
    expect(fs.readdirSync(incoming)).toEqual(["picked.zip"]);
    expect(fs.readFileSync(copy, "utf8")).toBe("PK zip");
  });

  it("names the local day, which is what 'first launch of a day' compares", () => {
    expect(localDay(new Date(2026, 9, 3, 23, 59))).toBe("2026-10-03");
  });
});
