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
import { PayeeCategorisation } from "./PayeeCategorisation";
import { Payees } from "./Payees";
import { Rules } from "./Rules";

const HOUSEHOLD = {
  id: "house-1",
  name: "Casa",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
} as unknown as Household;

/** The fixtures' own words. */
const DATA = /^(Casa|Doe|Sam|Bakery|Cinema|Everyday|Groceries|Bills|Rent|Water|Amazon|Carrefour|Square|Santander|Bar|Marisol|SQ|WWW|AMAZON|COMPRA|INTERNET|PAGO|MOVIL|MARISOL|MADRID|CARREFOUR|CINEMA|BAKERY)$/;

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

  it("Payees, with spellings to merge, and the merge dialogs", async () => {
    const bakery = { id: "p1", name: "Bakery", transfer_account_id: null, transaction_count: 3, rule_count: 1 };
    const cinema = { id: "p2", name: "Cinema", transfer_account_id: null, transaction_count: 1, rule_count: 0 };
    const leg = { id: "p3", name: "Sam", transfer_account_id: "a1", transaction_count: 2, rule_count: 0 };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/payee-collisions")) return [{ key: "bakery", payees: [bakery, cinema] }];
      if (path.includes("/accounts")) return [{ id: "a1", name: "Doe", currency: "EUR" }];
      return [bakery, cinema, leg];
    });
    render(withQueries(<Payees household={HOUSEHOLD} />));
    await screen.findByText("Sam");
    expect(left()).toEqual([]);

    // Review the spellings, then merge one payee into another.
    fireEvent.click(document.querySelector("section.card td button")!);
    expect(left()).toEqual([]);
    cleanup();
    render(withQueries(<Payees household={HOUSEHOLD} />));
    await screen.findByText("Sam");
    const merge = Array.from(document.querySelectorAll(".card")).pop()!.querySelector("tbody td button")!;
    fireEvent.click(merge);
    expect(left()).toEqual([]);
  });

  it("Payee categorisation, with a breakdown", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.includes("/stats/payees"))
        return {
          payees: [
            {
              payee_id: "p1",
              transaction_count: 1234,
              category_count: 5,
              categories: ["Groceries", "Rent", "Water", "Bills", "Everyday"].map((name, at) => ({
                key: `c${at}`,
                name,
                transaction_count: 10 - at,
              })),
            },
          ],
        };
      return [{ id: "p1", name: "Bakery", transfer_account_id: null, transaction_count: 1234, rule_count: 0 }];
    });
    render(withQueries(<PayeeCategorisation household={HOUSEHOLD} />));
    await screen.findByText("Bakery");
    expect(left()).toEqual([]);
    fireEvent.click(document.querySelector("button.tally-more")!);
    expect(left()).toEqual([]);
  });

  it("Payee naming rules, a new rule of both kinds, and applying them", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/payee-rules"))
        return [
          { id: "r1", match_type: "contains", action: "map", pattern: "BAKERY", payee_id: "p1", replacement: null, priority: 1, enabled: true },
          { id: "r2", match_type: "prefix", action: "rewrite", pattern: "SQ", payee_id: null, replacement: null, priority: 2, enabled: false },
        ];
      if (path.endsWith("/payee-suggestions"))
        return [{ pattern: "CINEMA", match_type: "contains", strings: 3, transactions: 9, payees: 3, examples: ["CINEMA 1", "CINEMA 2"] }];
      return [{ id: "p1", name: "Bakery", transfer_account_id: null, transaction_count: 3, rule_count: 1 }];
    });
    vi.mocked(api.post).mockResolvedValue({ considered: 40, changing: 3, moves: [{ to_name: "Bakery" }], orphaned: ["x"] });
    render(withQueries(<Rules household={HOUSEHOLD} />));
    await screen.findByText("BAKERY");
    expect(left()).toEqual([]);

    // The new-rule panel, naming a payee and then taking a rail off.
    fireEvent.click(document.querySelector("h1 + button")!);
    expect(left()).toEqual([]);
    const action = document.querySelectorAll(".panel select")[0] as HTMLSelectElement;
    fireEvent.change(action, { target: { value: "rewrite" } });
    expect(left()).toEqual([]);
    cleanup();

    // Applying the rules to what is already here.
    render(withQueries(<Rules household={HOUSEHOLD} />));
    await screen.findByText("CINEMA");
    fireEvent.click(document.querySelector(".card .row .small-button")!);
    await screen.findByText("x", { exact: false }).catch(() => undefined);
    await new Promise((done) => setTimeout(done, 0));
    expect(left()).toEqual([]);
  });
});
