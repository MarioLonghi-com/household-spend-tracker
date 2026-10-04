// @vitest-environment jsdom

/**
 * "The file itself" is numbered the way the preview's Line column is (#128).
 *
 * The parser (`statements/parsing.py`) drops blank lines, then numbers the csv
 * records that are left from 1, title block and header included. Numbering
 * every physical line would put the gutter one ahead of the table per blank
 * line above the row being checked -- which is the whole reason for the gutter.
 *
 * The line numbers the preview gives this file (4, 5, 6) are what the parser
 * returns for it; `tests/test_statement_formats.py` pins that on the server
 * side with the same text, so the two cannot drift apart unseen.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { RAW_TEXT_LIMIT, RawFile, numberRawLines } from "./Import";

afterEach(cleanup);

const FILE = [
  "Bank of Nowhere, current account", //  1
  "Statement for January 2026", //        2
  "", //                                  blank: no number
  "Date,Payee,Amount", //                 3
  "2026-01-02,Alpha Grocer,-12.50", //    4
  "", //                                  blank
  "   ", //                               only spaces: blank to the parser too
  '2026-01-05,"Beta Books', //            5
  'second line of the payee",-8.00', //   same record as 5: no number
  "2026-01-09,Gamma Cafe,-3.20", //       6
].join("\n");

/** Line -> the start of that row, as `parsing.parse` numbers this file. */
const PREVIEW_LINES: Record<number, string> = {
  4: "2026-01-02,Alpha Grocer",
  5: "2026-01-05,",
  6: "2026-01-09,Gamma Cafe",
};

function show(text: string, kind = "table") {
  render(<RawFile raw={{ name: "jan.csv", text, total: text.length }} kind={kind} delimiter="," />);
  const pre = document.querySelector<HTMLPreElement>("pre.raw-file")!;
  const numbered = [...pre.querySelectorAll<HTMLElement>(".raw-line[data-line]")];
  return { pre, numbered };
}

describe("numbering the file like the preview", () => {
  it("skips blank lines and carries on counting after them", () => {
    expect(numberRawLines(FILE, ",").map((line) => line.no)).toEqual([
      1, 2, null, 3, 4, null, null, 5, null, 6,
    ]);
  });

  it("puts preview line N and gutter number N on the same line of the file", () => {
    const { numbered } = show(FILE);
    for (const [no, start] of Object.entries(PREVIEW_LINES)) {
      const line = numbered.find((element) => element.dataset.line === no);
      expect(line?.textContent).toMatch(new RegExp(`^${start}`));
    }
    expect(numbered.map((element) => element.dataset.line)).toEqual(["1", "2", "3", "4", "5", "6"]);
  });

  it("keeps the numbers out of the text, so a copy is the file and nothing else", () => {
    const { pre } = show(FILE);
    // The numbers are CSS generated content, which is never in the text.
    expect(pre.textContent).toBe(FILE);
  });

  it("numbers a Windows file the same way", () => {
    expect(numberRawLines(FILE.replace(/\n/g, "\r\n"), ",")).toEqual(numberRawLines(FILE, ","));
  });

  it("stops the numbers where a cut-off view stops the text", () => {
    const row = "2026-01-02,Alpha Grocer,-12.50";
    const text = ["Date,Payee,Amount", ...Array.from({ length: 1000 }, () => row)].join("\n");
    expect(text.length).toBeGreaterThan(RAW_TEXT_LIMIT);

    const { pre, numbered } = show(text);
    expect(pre.textContent).toBe(text.slice(0, RAW_TEXT_LIMIT));
    // Every line the view shows any of is numbered, and none past it.
    const lastShown = text.slice(0, RAW_TEXT_LIMIT).split("\n").length;
    expect(numbered).toHaveLength(lastShown);
    expect(numbered.at(-1)!.dataset.line).toBe(String(lastShown));
  });

  it("leaves an OFX file unnumbered, since its Line counts transactions", () => {
    const { pre, numbered } = show("OFXHEADER:100\nDATA:OFXSGML\n\n<OFX>\n</OFX>", "ofx");
    expect(numbered).toHaveLength(0);
    expect(pre.classList.contains("numbered")).toBe(false);
  });
});
