/**
 * English says "1 row", not "1 rows" (#269).
 *
 * While the screens were extracted, a count kept its English byte for byte,
 * wrong plural included, so nothing visible changed. #269 fixed them on
 * purpose. Each case renders the English catalog's own message -- the one the
 * build serves -- at 0, 1 and 2, so a regression to one form shows as a
 * changed sentence here.
 */
import { describe, expect, it } from "vitest";
import { generateMessageId } from "@lingui/message-utils/generateMessageId";
import { i18n } from "../lib/i18n";
import { readPo } from "./catalogs.test";
import english from "./en/messages.po?raw";

const catalog = readPo(english);

/** The English message with this source, rendered with these values. */
function say(source: string, values: Record<string, unknown>): string {
  const entry = catalog.find((one) => one.id === source);
  if (!entry) throw new Error(`not in the English catalog: ${source}`);
  return i18n._(generateMessageId(entry.id, entry.context), values);
}

/** The usual shape: `{0}` picks the form, the later numbers show the count. */
function counted(n: number): Record<string, unknown> {
  return { 0: n, 1: String(n), 2: String(n), 3: String(n), 4: String(n), count: n, shown: String(n), wait: n };
}

const CASES: [string, (n: number) => Record<string, unknown>, [string, string, string]][] = [
  [
    "{count, plural, one {has {count} receipt} other {has {count} receipts}}",
    counted,
    ["has 0 receipts", "has 1 receipt", "has 2 receipts"],
  ],
  [
    "{count, plural, one {{count} page} other {{count} pages}}",
    counted,
    ["0 pages", "1 page", "2 pages"],
  ],
  [
    "{count, plural, one {{shown} file} other {{shown} files}}",
    counted,
    ["0 files", "1 file", "2 files"],
  ],
  [
    "{count, plural, one {{shown} row} other {{shown} rows}}",
    counted,
    ["0 rows", "1 row", "2 rows"],
  ],
  [
    "{count, plural, one {{shown} transaction matches the filters} other {{shown} transactions match the filters}}",
    counted,
    ["0 transactions match the filters", "1 transaction matches the filters", "2 transactions match the filters"],
  ],
  [
    "{0, plural, one {{1} line} other {{2} lines}}",
    counted,
    ["0 lines", "1 line", "2 lines"],
  ],
  [
    "{0, plural, one {{1} character} other {{2} characters}}",
    counted,
    ["0 characters", "1 character", "2 characters"],
  ],
  [
    "{wait, plural, one {Try again in {wait} second.} other {Try again in {wait} seconds.}}",
    counted,
    ["Try again in 0 seconds.", "Try again in 1 second.", "Try again in 2 seconds."],
  ],
  [
    "{0, plural, one {{1} leg} other {{2} legs}}",
    counted,
    ["0 legs", "1 leg", "2 legs"],
  ],
  [
    "{0, plural, one {{1} package.} other {{2} packages.}}",
    counted,
    ["0 packages.", "1 package.", "2 packages."],
  ],
  [
    "{0, plural, one {Its {1} row is left out and listed in the report.} other {Its {2} rows are left out and listed in the report.}}",
    counted,
    [
      "Its 0 rows are left out and listed in the report.",
      "Its 1 row is left out and listed in the report.",
      "Its 2 rows are left out and listed in the report.",
    ],
  ],
  [
    "{0, plural, one {Tick the {1} row the filter shows} other {Tick all {2} rows the filter shows}}",
    counted,
    ["Tick all 0 rows the filter shows", "Tick the 1 row the filter shows", "Tick all 2 rows the filter shows"],
  ],
  [
    "{0, plural, one {Move {1} row} other {Move {2} rows}}",
    counted,
    ["Move 0 rows", "Move 1 row", "Move 2 rows"],
  ],
  [
    "{0, plural, one {Also delete the payee left empty} other {Also delete the {1} payees left empty}}",
    counted,
    ["Also delete the 0 payees left empty", "Also delete the payee left empty", "Also delete the 2 payees left empty"],
  ],
  [
    "{0, plural, one {in {1} group} other {in {2} groups}}",
    counted,
    ["in 0 groups", "in 1 group", "in 2 groups"],
  ],
  [
    "{0, plural, one {in {1} group, {2} archived} other {in {3} groups, {4} archived}}",
    (n) => ({ 0: n, 1: String(n), 2: "5", 3: String(n), 4: "5" }),
    ["in 0 groups, 5 archived", "in 1 group, 5 archived", "in 2 groups, 5 archived"],
  ],
  [
    "{0, plural, one {Nothing would change. {1} row carries the bank’s words and today’s rules already agree with it.} other {Nothing would change. {2} rows carry the bank’s words and today’s rules already agree with all of them.}}",
    counted,
    [
      "Nothing would change. 0 rows carry the bank’s words and today’s rules already agree with all of them.",
      "Nothing would change. 1 row carries the bank’s words and today’s rules already agree with it.",
      "Nothing would change. 2 rows carry the bank’s words and today’s rules already agree with all of them.",
    ],
  ],
  [
    "<0>{0}</0> of {1, plural, one {{2} row would move.} other {{3} rows would move.}}",
    (n) => ({ 0: "0", 1: n, 2: String(n), 3: String(n) }),
    ["<0>0</0> of 0 rows would move.", "<0>0</0> of 1 row would move.", "<0>0</0> of 2 rows would move."],
  ],
  [
    "It rewrites <0>{0}</0> of {1, plural, one {{2} row already in this ledger.} other {{3} rows already in this ledger.}}",
    (n) => ({ 0: "0", 1: n, 2: String(n), 3: String(n) }),
    [
      "It rewrites <0>0</0> of 0 rows already in this ledger.",
      "It rewrites <0>0</0> of 1 row already in this ledger.",
      "It rewrites <0>0</0> of 2 rows already in this ledger.",
    ],
  ],
  [
    "It claims <0>{0}</0> of {1, plural, one {{2} row already in this ledger.} other {{3} rows already in this ledger.}}",
    (n) => ({ 0: "0", 1: n, 2: String(n), 3: String(n) }),
    [
      "It claims <0>0</0> of 0 rows already in this ledger.",
      "It claims <0>0</0> of 1 row already in this ledger.",
      "It claims <0>0</0> of 2 rows already in this ledger.",
    ],
  ],
  [
    "{0, plural, one {{1} more currency: {2}. {3} shown.} other {{4} more currencies: {5}. {6} shown.}}",
    (n) => ({ 0: n, 1: String(n), 2: "CHF", 3: "0", 4: String(n), 5: "CHF, SEK", 6: "0" }),
    ["0 more currencies: CHF, SEK. 0 shown.", "1 more currency: CHF. 0 shown.", "2 more currencies: CHF, SEK. 0 shown."],
  ],
  [
    "{0, plural, one {{1} of {2} months has any transactions — an empty column is a month with nothing imported, not a month you spent nothing.} other {{3} of {4} months have any transactions — an empty column is a month with nothing imported, not a month you spent nothing.}}",
    (n) => ({ 0: n, 1: String(n), 2: "6", 3: String(n), 4: "6" }),
    [
      "0 of 6 months have any transactions — an empty column is a month with nothing imported, not a month you spent nothing.",
      "1 of 6 months has any transactions — an empty column is a month with nothing imported, not a month you spent nothing.",
      "2 of 6 months have any transactions — an empty column is a month with nothing imported, not a month you spent nothing.",
    ],
  ],
];

describe("English counts take the singular for one (#269)", () => {
  it.each(CASES)("%s", (source, values, [zero, one, two]) => {
    expect([say(source, values(0)), say(source, values(1)), say(source, values(2))]).toEqual([zero, one, two]);
  });

  it("leaves no English plural with only an `other` form", () => {
    const lonely = catalog.filter((one) => /\{\w+, plural, other \{/.test(one.id)).map((one) => one.id);
    expect(lonely).toEqual([]);
  });

  it("leaves no `one` form saying a plural noun after its count", () => {
    // "one {{1} legs}", "one {has {count} receipts}": the shape #269 removed.
    const wrong = catalog
      .map((one) => one.id)
      .filter((id) => /one \{[^{}]*\{\w+\} (rows|lines|legs|pages|packages|files|receipts|seconds)\b/.test(id));
    expect(wrong).toEqual([]);
  });
});
