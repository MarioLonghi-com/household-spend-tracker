// @vitest-environment jsdom

/**
 * The payee screen's "spellings of one name" panel (#268).
 *
 * Payees that differ only in accents, dashes or invisible spaces are listed,
 * sortable at their headings like every list, and merged only when somebody
 * chooses the spelling to keep -- through the existing per-payee merge, one
 * call per spelling folded away. Every name is invented.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Payees } from "./Payees";
import type { Household, Payee, PayeeCollision } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

function payee(id: string, name: string, transactions: number): Payee {
  return { id, name, transfer_account_id: null, transaction_count: transactions, rule_count: 0 };
}

const CAFE = [payee("p1", "Café Sol", 2), payee("p2", "CAFE SOL", 9)];
const BAKERY = [
  payee("p3", "Panadería Río", 1),
  payee("p4", "PANADERIA RIO", 1),
  payee("p5", "Panaderia\u00a0Rio", 30),
];
const GROUPS: PayeeCollision[] = [
  { key: "cafe sol", payees: CAFE },
  { key: "panaderia rio", payees: BAKERY },
];

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Payees household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

describe("Spellings of one name", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) => {
        if (url.endsWith("/payee-collisions")) return GROUPS as never;
        if (url.endsWith("/payees")) return [...CAFE, ...BAKERY] as never;
        return [] as never;
      });
    vi.mocked(api.post)
      .mockReset()
      .mockImplementation(async (_url: string, body?: unknown) => {
        const into = (body as { into_payee_id: string }).into_payee_id;
        return [...CAFE, ...BAKERY].find((one) => one.id === into) as never;
      });
  });

  it("lists each group and sorts at its headings", async () => {
    mount();
    const panel = await screen.findByLabelText("Spellings of one name");
    const table = within(panel).getByRole("table");
    const order = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getAllByRole("cell")[1].textContent);

    // By the shared name, A to Z, to begin with.
    expect(order()).toEqual(["11", "32"]);
    fireEvent.click(within(table).getByRole("button", { name: /Spellings/ }));
    expect(order()).toEqual(["32", "11"]);
    fireEvent.click(within(table).getByRole("button", { name: /Transactions/ }));
    expect(order()).toEqual(["11", "32"]);
    fireEvent.click(within(table).getByRole("button", { name: /Transactions/ }));
    expect(order()).toEqual(["32", "11"]);
    expect(api.post).not.toHaveBeenCalled();
  });

  it("merges every other spelling into the one chosen, and only on the button", async () => {
    mount();
    const panel = await screen.findByLabelText("Spellings of one name");
    const bakery = within(panel)
      .getAllByRole("row")
      .find((row) => row.textContent?.includes("Panadería Río"))!;
    fireEvent.click(within(bakery).getByRole("button", { name: "Review…" }));

    const dialog = await screen.findByRole("dialog");
    // The busiest spelling is offered, not merged into.
    const busiest = within(dialog).getByRole("radio", { name: /Panaderia\u00a0Rio/ });
    expect((busiest as HTMLInputElement).checked).toBe(true);
    expect(api.post).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("radio", { name: /PANADERIA RIO/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge into PANADERIA RIO" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.post).mock.calls).toEqual([
      ["/payees/p3/merge", { into_payee_id: "p4" }],
      ["/payees/p5/merge", { into_payee_id: "p4" }],
    ]);
  });

  it("after a merge fails half-way, shows the error and retries only what is left", async () => {
    // The server's view: p3 is merged away by the first call, the second fails.
    let gone: string[] = [];
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      const live = (one: Payee) => !gone.includes(one.id);
      if (url.endsWith("/payee-collisions"))
        return GROUPS.map((group) => ({ ...group, payees: group.payees.filter(live) })) as never;
      if (url.endsWith("/payees")) return [...CAFE, ...BAKERY].filter(live) as never;
      return [] as never;
    });
    vi.mocked(api.post)
      .mockImplementationOnce(async (url: string) => {
        gone = [...gone, url.split("/")[2]];
        return BAKERY[1] as never;
      })
      .mockImplementationOnce(async () => {
        throw new Error("the database is busy");
      })
      .mockImplementationOnce(async () => BAKERY[1] as never);

    mount();
    const panel = await screen.findByLabelText("Spellings of one name");
    const bakery = within(panel)
      .getAllByRole("row")
      .find((row) => row.textContent?.includes("Panadería Río"))!;
    fireEvent.click(within(bakery).getByRole("button", { name: "Review…" }));
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("radio", { name: /PANADERIA RIO/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge into PANADERIA RIO" }));

    // The error is on screen, and the group is what the server now says.
    expect(await screen.findByText("the database is busy")).toBeTruthy();
    await waitFor(() =>
      expect(within(screen.getByRole("dialog")).queryByRole("radio", { name: /Panadería Río/ })).toBeNull(),
    );
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByRole("radio")).toHaveLength(2);

    fireEvent.click(within(dialog).getByRole("radio", { name: /PANADERIA RIO/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Merge into PANADERIA RIO" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.post).mock.calls).toEqual([
      ["/payees/p3/merge", { into_payee_id: "p4" }],
      ["/payees/p5/merge", { into_payee_id: "p4" }],
      // Not p3 again: it is gone.
      ["/payees/p5/merge", { into_payee_id: "p4" }],
    ]);
  });

  it("draws nothing when no payees share a name", async () => {
    vi.mocked(api.get).mockImplementation(async (url: string) =>
      (url.endsWith("/payees") ? CAFE.slice(0, 1) : []) as never,
    );
    mount();
    await screen.findByText("Café Sol");
    expect(screen.queryByLabelText("Spellings of one name")).toBeNull();
  });
});
