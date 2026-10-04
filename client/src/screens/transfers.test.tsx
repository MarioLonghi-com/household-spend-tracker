// @vitest-environment jsdom

/**
 * The Transfers screen (#70) and the How-import-works page (#73).
 *
 * The first is asserted on what it sends: "Link all" carries every strong
 * pair, a row's "Link" carries that pair alone, "Link N selected" carries the
 * ticked pairs in one request (#126), and the table sorts at its headings. The second on what it says: every outcome the preview can show,
 * in the preview's own words, and nothing a person could press.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Transfers } from "./Transfers";
import { ImportGuide } from "./ImportGuide";
import type { Household } from "../lib/types";

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

function leg(id: string, account: string, date: string, amount: number, currency = "EUR") {
  return {
    id,
    account_id: `acct-${account}`,
    account_name: account,
    date,
    amount,
    currency,
    description: `words ${id}`,
  };
}

const FINDINGS = {
  strong: [
    { out_leg: leg("o2", "Saver", "2026-03-24", -500), in_leg: leg("i2", "Current", "2026-03-24", 500), strength: "strong", why: "the Saver row names Current" },
    { out_leg: leg("o1", "Current", "2026-03-01", -200), in_leg: leg("i1", "Card", "2026-03-02", 200), strength: "strong", why: "the Current row names Card" },
  ],
  suggested: [
    { out_leg: leg("o3", "Current", "2026-04-01", -42), in_leg: leg("i3", "Saver", "2026-04-01", 42), strength: "suggested", why: "the amounts match and the dates are close" },
  ],
  awaiting: [{ leg: leg("w1", "Current", "2026-04-02", 1500), why: "names a household member; its other side is not in the ledger yet" }],
};

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("Transfers", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset().mockImplementation(async (url: string) => (url.endsWith("/categories") ? [] : FINDINGS));
    vi.mocked(api.post).mockReset().mockResolvedValue({ linked: 2 });
  });

  it("links every strong pair in one request", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    fireEvent.click(await screen.findByRole("button", { name: "Link all 2" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(api.post).toHaveBeenCalledWith("/households/house-1/transfers/link", {
      pairs: [
        { first_id: "o2", second_id: "i2" },
        { first_id: "o1", second_id: "i1" },
      ],
      by: "evidence",
    });
  });

  it("links one suggested pair on its own", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    const suggestion = (await screen.findByText("the amounts match and the dates are close")).closest("tr")!;
    fireEvent.click(within(suggestion).getByRole("button", { name: "Link" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0][1]).toEqual({ pairs: [{ first_id: "o3", second_id: "i3" }] });
  });

  it("sorts the pairs by the outgoing date, and turns round at the heading", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    await screen.findByText("the Saver row names Current");
    const table = screen.getAllByRole("table")[0];
    const order = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getByText(/names/).textContent);
    expect(order()).toEqual(["the Current row names Card", "the Saver row names Current"]);
    fireEvent.click(within(table).getByRole("button", { name: /Out of/ }));
    expect(order()).toEqual(["the Saver row names Current", "the Current row names Card"]);
  });

  it("keeps the currency groups still when Amount turns round, base currency first", async () => {
    // The five pairs from #129. Sorting by the tuple flipped the groups with
    // the figures, so a descending column read "£90 £15 €70 €20 €5".
    const pair = (n: number, currency: string, amount: number) => ({
      out_leg: leg(`mo${n}`, currency === "EUR" ? "Current" : "Sterling", `2026-05-0${n}`, -amount, currency),
      in_leg: leg(`mi${n}`, currency === "EUR" ? "Saver" : "Pocket", `2026-05-0${n}`, amount, currency),
      strength: "suggested",
      why: `mixed ${n}`,
    });
    const mixed = {
      ...FINDINGS,
      suggested: [pair(1, "GBP", 1500), pair(2, "EUR", 7000), pair(3, "GBP", 9000), pair(4, "EUR", 500), pair(5, "EUR", 2000)],
    };
    vi.mocked(api.get).mockImplementation(async (url: string) => (url.endsWith("/categories") ? [] : mixed));
    mount(<Transfers household={HOUSEHOLD} />);
    const table = (await screen.findByText("mixed 1")).closest("table")!;
    const amounts = () =>
      [...table.querySelectorAll('td[data-label="Amount"]')].map((cell) => cell.textContent);
    const heading = within(table).getByRole("button", { name: /Amount/ });
    fireEvent.click(heading);
    expect(amounts()).toEqual(["€70.00", "€20.00", "€5.00", "£90.00", "£15.00"]);
    fireEvent.click(heading);
    expect(amounts()).toEqual(["€5.00", "€20.00", "€70.00", "£15.00", "£90.00"]);
  });

  it("lists the legs still waiting for the other statement", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    expect(await screen.findByText(/its other side is not in the ledger yet/)).toBeTruthy();
  });
});

describe("Transfers, ticking several pairs (#126)", () => {
  // Four suggestions. The first two share their outgoing row (o3 could have
  // gone to Saver or to Card), so at most one of them can ever be linked.
  const SHARED = {
    ...FINDINGS,
    suggested: [
      { out_leg: leg("o3", "Current", "2026-04-01", -42), in_leg: leg("i3", "Saver", "2026-04-01", 42), strength: "suggested", why: "o3 to Saver" },
      { out_leg: leg("o3", "Current", "2026-04-01", -42), in_leg: leg("i4", "Card", "2026-04-02", 42), strength: "suggested", why: "o3 to Card" },
      { out_leg: leg("o5", "Card", "2026-04-03", -77), in_leg: leg("i5", "Saver", "2026-04-03", 77, "GBP"), strength: "suggested", why: "o5 to Saver" },
      { out_leg: leg("o6", "Saver", "2026-04-05", -13), in_leg: leg("i6", "Current", "2026-04-06", 13), strength: "suggested", why: "o6 to Current" },
    ],
  };
  const TIP = "Shares a row with a pair you've ticked";

  beforeEach(() => {
    vi.mocked(api.get).mockReset().mockImplementation(async (url: string) => (url.endsWith("/categories") ? [] : SHARED));
    vi.mocked(api.post).mockReset().mockResolvedValue({ linked: 3 });
  });

  async function suggested() {
    const card = (await screen.findByText("o3 to Saver")).closest(".card") as HTMLElement;
    const box = (label: string) => within(card).getByRole("checkbox", { name: label }) as HTMLInputElement;
    return {
      card,
      toSaver: () => box("Select Current to Saver, 2026-04-01"),
      toCard: () => box("Select Current to Card, 2026-04-01"),
      fromCard: () => box("Select Card to Saver, 2026-04-03"),
      fromSaver: () => box("Select Saver to Current, 2026-04-05"),
      all: () => box("Select every pair that does not share a row with another"),
    };
  }
  // Which pairs are ticked, by name: the row order is the table's opening
  // sort ("Worth a look" opens on Why), and the assertions are not about it.
  function ticks(table: Awaited<ReturnType<typeof suggested>>) {
    return {
      toSaver: table.toSaver().checked,
      toCard: table.toCard().checked,
      fromCard: table.fromCard().checked,
      fromSaver: table.fromSaver().checked,
    };
  }

  it("links three ticked pairs in one request, then clears the ticks", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    const table = await suggested();
    expect((within(table.card).getByRole("button", { name: "Link selected" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(table.toSaver());
    fireEvent.click(table.fromCard());
    fireEvent.click(table.fromSaver());
    fireEvent.click(within(table.card).getByRole("button", { name: "Link 3 selected" }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith("/households/house-1/transfers/link", {
      pairs: [
        { first_id: "o3", second_id: "i3" },
        { first_id: "o5", second_id: "i5" },
        { first_id: "o6", second_id: "i6" },
      ],
    });
    // The success reloads the findings, and a reload clears every tick.
    await waitFor(() => expect(ticks(table)).toEqual({ toSaver: false, toCard: false, fromCard: false, fromSaver: false }));
    expect((within(table.card).getByRole("button", { name: "Link selected" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("disables a pair that shares a row with a ticked one, and enables it again", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    const table = await suggested();

    fireEvent.click(table.toSaver());
    expect(table.toCard().disabled).toBe(true);
    expect(table.toCard().title).toBe(TIP);
    expect(table.toCard().closest("td")!.title).toBe(TIP);
    // A pair with rows of its own is left alone.
    expect(table.fromCard().disabled).toBe(false);
    expect(table.fromCard().title).toBe("");

    // Clicking the disabled one does nothing: the two can never both be ticked.
    fireEvent.click(table.toCard());
    expect(ticks(table)).toEqual({ toSaver: true, toCard: false, fromCard: false, fromSaver: false });

    fireEvent.click(table.toSaver());
    expect(table.toCard().disabled).toBe(false);
    expect(table.toCard().title).toBe("");

    // And the other way round.
    fireEvent.click(table.toCard());
    expect(table.toSaver().disabled).toBe(true);
    expect(ticks(table)).toEqual({ toSaver: false, toCard: true, fromCard: false, fromSaver: false });
  });

  it("selects every pair that does not clash, the higher one winning, and clears them again", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    const table = await suggested();

    // Opening on Why puts "o3 to Card" above "o3 to Saver", so Card wins.
    fireEvent.click(table.all());
    expect(ticks(table)).toEqual({ toSaver: false, toCard: true, fromCard: true, fromSaver: true });
    expect(table.toSaver().disabled).toBe(true);
    expect(table.all().checked).toBe(true);
    expect(within(table.card).getByRole("button", { name: "Link 3 selected" })).toBeTruthy();

    fireEvent.click(table.all());
    expect(ticks(table)).toEqual({ toSaver: false, toCard: false, fromCard: false, fromSaver: false });

    // What is already ticked stays ticked: select-all adds around it, even
    // where the one it would have chosen sits higher up.
    fireEvent.click(table.toSaver());
    fireEvent.click(table.all());
    expect(ticks(table)).toEqual({ toSaver: true, toCard: false, fromCard: true, fromSaver: true });
    expect(table.toCard().disabled).toBe(true);
  });

  it("clears the ticks when the findings reload for any other reason", async () => {
    mount(<Transfers household={HOUSEHOLD} />);
    const table = await suggested();
    fireEvent.click(table.fromCard());
    expect(ticks(table)).toEqual({ toSaver: false, toCard: false, fromCard: true, fromSaver: false });

    // Linking the strong pairs reloads the findings under the suggestions.
    fireEvent.click(screen.getByRole("button", { name: "Link all 2" }));
    await waitFor(() => expect(ticks(table)).toEqual({ toSaver: false, toCard: false, fromCard: false, fromSaver: false }));
  });
});

describe("How import works", () => {
  it("names every outcome in the words the preview uses, and has nothing to press", () => {
    const { container } = mount(<ImportGuide />);
    const pills = [...container.querySelectorAll(".pill")].map((pill) => pill.textContent);
    expect(pills).toEqual([
      "New",
      "Already have it",
      "Already imported",
      "Needs a look",
      "Could not read",
      "Not for this account",
    ]);
    expect(screen.queryAllByRole("button")).toEqual([]);
    expect(screen.queryAllByRole("textbox")).toEqual([]);
  });
});
