// @vitest-environment jsdom

/**
 * The Import preview is windowed, and still sorts the whole file.
 *
 * A multi-year statement is thousands of lines and every one went into the
 * DOM at once, which froze the tab (#108). The window must not change what
 * sorting means, though: every list sorts at its column headers, and a sort
 * that only reordered the 250 lines on screen would put a file's largest
 * amount wherever the first window happened to end. So these count the lines
 * on the page and check which line comes first after a heading is clicked.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    upload: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { STEP } from "../lib/useWindowed";
import { Import } from "./Import";
import type { StagedImport } from "./Import";
import type { Household, ImportLine, ImportPreview } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  colours: null,
} as unknown as Household;

const LINES = 600;

function line(n: number): ImportLine {
  return {
    id: `line-${n}`,
    line_no: n,
    raw: `raw ${n}`,
    // The amount grows with the line number, so the largest is the last line
    // of the file -- well outside the first window.
    parsed: { date: "2026-01-05", payee: `Payee ${n}`, amount: -n * 10 },
    outcome: "created",
    transaction_id: null,
    reason: null,
    category_id: null,
    category_name: null,
    category_chosen: false,
    similar_lines: 0,
  } as ImportLine;
}

const STAGED: StagedImport = {
  batch_id: "batch-1",
  filename: "years.csv",
  account_id: "acct-1",
  account_name: "Current",
  actor_name: "Jane",
  staged_at: "2026-09-20T09:00:00Z",
  row_count: LINES,
  sha256: "sha",
};

const PREVIEW: ImportPreview = {
  batch_id: "batch-1",
  filename: "years.csv",
  account_id: "acct-1",
  sha256: "sha",
  detected: {},
  warnings: [],
  counts: { created: LINES },
  lines: Array.from({ length: LINES }, (_, index) => line(index + 1)),
};

beforeEach(() => {
  // jsdom has no IntersectionObserver; the button is the guarantee anyway.
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.endsWith("/imports")) return Promise.resolve([STAGED]);
    if (path.endsWith("/imports/batch-1")) return Promise.resolve(PREVIEW);
    if (path.endsWith("/accounts"))
      return Promise.resolve([{ id: "acct-1", name: "Current", currency: "EUR" }]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

afterEach(() => vi.unstubAllGlobals());

const shownLines = () =>
  [...document.querySelectorAll<HTMLInputElement>('input[aria-label^="Include line "]')].map(
    (box) => Number(box.getAttribute("aria-label")!.replace("Include line ", "")),
  );

async function openPreview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Import household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Open" }, { timeout: 10_000 }));
  await waitFor(() => expect(shownLines().length).toBeGreaterThan(0), { timeout: 20_000 });
}

// 250 preview rows, each with its category editor, take seconds to mount in
// jsdom on a slow machine; the default five-second budget is about the DOM,
// not about the code under test.
const SLOW = { timeout: 30_000 };

describe("the import preview", () => {
  it("puts one window of lines on the page, not the whole file", SLOW, async () => {
    await openPreview();

    expect(shownLines()).toHaveLength(STEP);
    const more = document.querySelector<HTMLButtonElement>("button.more-rows")!;
    expect(more.textContent).toMatch(/Showing 250 of 600/);

    fireEvent.click(more);
    expect(shownLines()).toHaveLength(2 * STEP);
  });

  it("sorts the whole file at its headings, not only the lines on screen", SLOW, async () => {
    await openPreview();
    expect(shownLines()[0]).toBe(1);

    // Amount sorts largest-first on the first click; with outgoings that is
    // the smallest spend, line 1. Twice puts the largest spend first.
    // By title rather than by role: an accessibility-tree query over 250 rows
    // is most of a second each in jsdom.
    const amount = () => document.querySelector<HTMLButtonElement>('button[title="Sort by Amount"]')!;
    fireEvent.click(amount());
    fireEvent.click(amount());

    expect(shownLines()[0]).toBe(LINES);
    expect(shownLines()).toHaveLength(STEP);
  });
});
