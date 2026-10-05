// @vitest-environment jsdom

/**
 * The preview's category cell has four states and three answers (#9).
 *
 * Emptying the box hands a line back to the suggestion; it cannot also mean
 * "no category", because for a payee with a usual category, or a line the bank
 * labelled as interest, the suggestion *is* a category. So "Uncategorised" is
 * an answer of its own, and the cell has to show it differently from a line
 * that is uncategorised only because nothing suggested anything.
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
import { Import, UNCATEGORISED, categoryOf } from "./Import";
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

const GROCERIES = { id: "cat-groceries", name: "Groceries", full_name: "Everyday: Groceries" };
const TRAVEL = { id: "cat-travel", name: "Travel", full_name: "Quality of Life: Travel" };

function line(n: number, payee: string, extra: Partial<ImportLine>): ImportLine {
  return {
    id: `line-${n}`,
    line_no: n,
    raw: `raw ${n}`,
    parsed: { date: "2026-01-05", payee, amount: -100 * n },
    outcome: "created",
    transaction_id: null,
    reason: null,
    category_id: null,
    category_name: null,
    category_chosen: false,
    category_uncategorised: false,
    similar_lines: 0,
    ...extra,
  } as ImportLine;
}

/** One line in each of the four states, two payees between them. */
const CHOSEN = line(1, "Corner Shop", {
  category_id: TRAVEL.id,
  category_name: TRAVEL.full_name,
  category_chosen: true,
});
const SUGGESTED = line(2, "Corner Shop", {
  category_id: GROCERIES.id,
  category_name: GROCERIES.full_name,
});
const MARKED = line(3, "Corner Shop", { category_chosen: true, category_uncategorised: true });
const NOTHING = line(4, "Market Hall", {});

const PREVIEW: ImportPreview = {
  batch_id: "batch-1",
  filename: "statement.csv",
  account_id: "acct-1",
  sha256: "sha",
  detected: {},
  warnings: [],
  counts: { created: 4 },
  lines: [CHOSEN, SUGGESTED, MARKED, NOTHING],
};

const STAGED: StagedImport = {
  batch_id: "batch-1",
  filename: "statement.csv",
  account_id: "acct-1",
  account_name: "Current",
  actor_name: "Jane",
  staged_at: "2026-09-20T09:00:00Z",
  row_count: 4,
  sha256: "sha",
};

beforeEach(() => {
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.mocked(api.get).mockReset();
  vi.mocked(api.patch).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.endsWith("/imports")) return Promise.resolve([STAGED]);
    if (path.endsWith("/imports/batch-1")) return Promise.resolve(PREVIEW);
    if (path.endsWith("/accounts"))
      return Promise.resolve([{ id: "acct-1", name: "Current", currency: "EUR" }]);
    if (path.includes("/categories"))
      return Promise.resolve([
        { id: "g1", name: "Everyday", categories: [GROCERIES] },
        { id: "g2", name: "Quality of Life", categories: [TRAVEL] },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

afterEach(() => vi.unstubAllGlobals());

async function openPreview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Import household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Open" }));
  await screen.findByLabelText("Include line 4");
}

/** The category cell's button on a given line: the one titled for it. */
function cell(lineNo: number): HTMLButtonElement {
  const row = screen.getByLabelText(`Include line ${lineNo}`).closest("tr")!;
  return row.querySelector<HTMLButtonElement>('button[title="Where this line will land"]')!;
}

describe("the category cell's states", () => {
  it("names each of the four in words, muting only the guess and the gap", () => {
    expect(categoryOf(CHOSEN)).toEqual({ text: TRAVEL.full_name, muted: false });
    expect(categoryOf(SUGGESTED)).toEqual({ text: GROCERIES.full_name, muted: true });
    expect(categoryOf(MARKED)).toEqual({ text: "uncategorised (chosen)", muted: false });
    expect(categoryOf(NOTHING)).toEqual({ text: "uncategorised", muted: true });
  });

  it("shows them distinctly on the preview", async () => {
    await openPreview();

    expect(cell(1).textContent).toBe(TRAVEL.full_name);
    expect(cell(1).querySelector(".muted")).toBeNull();
    expect(cell(2).textContent).toBe(GROCERIES.full_name);
    expect(cell(2).querySelector(".muted")).not.toBeNull();
    expect(cell(3).textContent).toBe("uncategorised (chosen)");
    expect(cell(3).querySelector(".muted")).toBeNull();
    expect(cell(4).textContent).toBe("uncategorised");
    expect(cell(4).querySelector(".muted")).not.toBeNull();
  });
});

describe("the category cell's answers", () => {
  it("offers Uncategorised and sends it as its own answer, not as an empty box", async () => {
    vi.mocked(api.patch).mockResolvedValue({
      ...SUGGESTED,
      category_id: null,
      category_name: null,
      category_chosen: true,
      category_uncategorised: true,
    });
    await openPreview();

    fireEvent.click(cell(2));
    const box = screen.getByRole("combobox", { name: "Category" });
    // Listed with the categories, so the keyboard reaches it the same way.
    expect(screen.getByRole("option", { name: UNCATEGORISED })).toBeTruthy();

    fireEvent.change(box, { target: { value: "Uncategorised" } });
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.patch).mock.calls[0]).toEqual([
      "/households/house-1/imports/batch-1/lines/line-2",
      { uncategorised: true },
    ]);
    await waitFor(() => expect(cell(2).textContent).toBe("uncategorised (chosen)"));
  });

  it("hands an uncategorised line back to the suggestion when the box is emptied", async () => {
    vi.mocked(api.patch).mockResolvedValue({
      ...MARKED,
      category_id: GROCERIES.id,
      category_name: GROCERIES.full_name,
      category_chosen: false,
      category_uncategorised: false,
    });
    await openPreview();

    fireEvent.click(cell(3));
    const box = screen.getByRole("combobox", { name: "Category" });
    expect((box as HTMLInputElement).value).toBe(UNCATEGORISED);
    expect(screen.getByText("Empty it to go back to the suggestion.")).toBeTruthy();

    fireEvent.change(box, { target: { value: "" } });
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.patch).mock.calls[0][1]).toEqual({
      category_id: null,
      clear_category: true,
    });
    await waitFor(() => expect(cell(3).textContent).toBe(GROCERIES.full_name));
  });

  it("does not resend Uncategorised for a line already marked so", async () => {
    await openPreview();

    fireEvent.click(cell(3));
    const box = screen.getByRole("combobox", { name: "Category" });
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() =>
      expect(screen.queryByRole("combobox", { name: "Category" })).toBeNull(),
    );
    expect(api.patch).not.toHaveBeenCalled();
    expect(cell(3).textContent).toBe("uncategorised (chosen)");
  });

  it("offers the rest of the payee for Uncategorised, without a rule to set", async () => {
    vi.mocked(api.patch).mockResolvedValue({
      ...SUGGESTED,
      category_id: null,
      category_name: null,
      category_chosen: true,
      category_uncategorised: true,
      similar_lines: 2,
    });
    await openPreview();

    fireEvent.click(cell(2));
    const box = screen.getByRole("combobox", { name: "Category" });
    fireEvent.change(box, { target: { value: "Uncategorised" } });
    fireEvent.keyDown(box, { key: "Enter" });

    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toMatch(/Leave them uncategorised too\?/);
    expect(screen.getByRole("button", { name: "Yes, all 3 of them" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /set the rule/ })).toBeNull();
  });
});
