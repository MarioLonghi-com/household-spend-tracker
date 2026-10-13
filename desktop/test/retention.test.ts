import { describe, expect, it } from "vitest";
import { isOurs, newestFirst, toRemove } from "../src/retention";

const at = (stamp: string) => `spendtracker-${stamp}Z.sqlite3`;

describe("which backups stay (#62)", () => {
  it("keeps every backup of the newest day, and the newest of each earlier one", () => {
    const names = [
      at("20261013T080000"),
      at("20261013T120000"),
      at("20261012T090000"),
      at("20261012T180000"),
      at("20261011T070000"),
    ];
    expect(toRemove(names)).toEqual([at("20261012T090000")]);
  });

  it("keeps thirty days that have a backup, then one a month for twelve months", () => {
    const names: string[] = [];
    // One a day from 1 Jan 2025 to 13 Oct 2026.
    for (let day = new Date(Date.UTC(2025, 0, 1)); day <= new Date(Date.UTC(2026, 9, 13)); day.setUTCDate(day.getUTCDate() + 1)) {
      names.push(at(`${day.toISOString().slice(0, 10).replaceAll("-", "")}T120000`));
    }
    const removed = new Set(toRemove(names));
    const kept = names.filter((name) => !removed.has(name)).sort();
    // 30 dailies: 14 Sep to 13 Oct 2026.
    expect(kept.filter((name) => name >= at("20260914T000000"))).toHaveLength(30);
    expect(kept).toContain(at("20260914T120000"));
    expect(kept).not.toContain(at("20260913T120000"));
    // Monthlies: the newest of each of the 12 newest months, Nov 2025 to Oct 2026.
    expect(kept.filter((name) => name < at("20260914T000000"))).toEqual([
      at("20251130T120000"),
      at("20251231T120000"),
      at("20260131T120000"),
      at("20260228T120000"),
      at("20260331T120000"),
      at("20260430T120000"),
      at("20260531T120000"),
      at("20260630T120000"),
      at("20260731T120000"),
      at("20260831T120000"),
    ]);
    expect(kept).not.toContain(at("20251031T120000"));
  });

  it("counts days that have a backup, so a month away does not empty the folder", () => {
    const names = [at("20260101T100000"), at("20260301T100000"), at("20261013T100000")];
    expect(toRemove(names)).toEqual([]);
  });

  it("orders a second backup in the same second after the first", () => {
    const names = [at("20261013T080000"), "spendtracker-20261013T080000Z-2.sqlite3", "spendtracker-20261013T080000Z-10.sqlite3"];
    expect(newestFirst(names)).toEqual(["spendtracker-20261013T080000Z-10.sqlite3", "spendtracker-20261013T080000Z-2.sqlite3", at("20261013T080000")]);
    expect(toRemove([...names, at("20261014T090000")])).toEqual(["spendtracker-20261013T080000Z-2.sqlite3", at("20261013T080000")]);
  });

  it("never touches anything it did not write", () => {
    const theirs = ["holiday.jpg", "spendtracker-20261013T120000Z.sqlite3.partial", "Spendtracker-20261013T120000Z.sqlite3", "notes.sqlite3"];
    expect(theirs.some(isOurs)).toBe(false);
    expect(toRemove([...theirs, at("20261012T080000"), at("20261012T090000"), at("20261013T090000")])).toEqual([at("20261012T080000")]);
  });
});
