// @vitest-environment jsdom

/**
 * #56's exit, screen by screen: no English left in en-XA on the remaining
 * screens. Each screen joins this file in the pull request that extracts it.
 * Names in the fixtures are data and pass through untouched.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { activate } from "../lib/i18n";
import type { Household } from "../lib/types";
import { untranslated } from "../test-pseudo";
import { Categories } from "./Categories";

const HOUSEHOLD = {
  id: "house-1",
  name: "Casa",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
} as unknown as Household;

/** The fixtures' own words. */
const DATA = /^(Casa|Doe|Sam|Bakery|Cinema|Everyday|Groceries|Bills|Rent|Water)$/;

function withQueries(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function left(root: HTMLElement = document.body): string[] {
  return untranslated(root).filter((word) => !DATA.test(word));
}

beforeEach(async () => {
  await activate("en-XA");
});

afterEach(async () => {
  cleanup();
  vi.clearAllMocks();
  await activate("en");
});

describe("in en-XA, the remaining screens show no English", () => {
  it("Categories, empty and full, and a category's panel", async () => {
    vi.mocked(api.get).mockResolvedValueOnce([]).mockResolvedValue({ categories: [] });
    const { unmount } = render(withQueries(<Categories household={HOUSEHOLD} />));
    await screen.findByRole("heading", { level: 1 });
    await new Promise((done) => setTimeout(done, 0));
    expect(left()).toEqual([]);
    unmount();

    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.includes("/stats/categories"))
        return {
          categories: [
            {
              category_id: "c1",
              transaction_count: 3,
              payee_count: 2,
              payees: [
                { key: "p1", name: "Bakery", transaction_count: 2 },
                { key: null, name: "Cinema", transaction_count: 1 },
              ],
              more_payees: 4,
            },
          ],
        };
      return [
        {
          id: "g1",
          name: "Everyday",
          sort_order: 0,
          categories: [
            { id: "c1", group_id: "g1", name: "Groceries", full_name: "Everyday: Groceries", sort_order: 0, archived: false, used_by: 3 },
            { id: "c2", group_id: "g1", name: "Rent", full_name: "Everyday: Rent", sort_order: 1, archived: false, used_by: 0 },
          ],
        },
        { id: "g2", name: "Bills", sort_order: 1, categories: [] },
      ];
    });
    render(withQueries(<Categories household={HOUSEHOLD} />));
    await screen.findByText("Groceries");
    expect(left()).toEqual([]);

    const row = screen.getByText("Groceries").closest("tr") as HTMLElement;
    fireEvent.click(row.querySelector("td.amount button.link")!);
    await screen.findByRole("dialog");
    expect(left()).toEqual([]);

    // The group's own panel, and a new category in it.
    fireEvent.click(document.querySelector("button.heading-link")!);
    expect(left()).toEqual([]);
    const addTo = document.querySelector(".card .row > button.link") as HTMLElement;
    fireEvent.click(addTo);
    expect(left()).toEqual([]);
  });
});
