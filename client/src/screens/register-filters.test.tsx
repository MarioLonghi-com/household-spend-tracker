// @vitest-environment jsdom

/**
 * The register's newer filters (#123), as the requests they send.
 *
 * Each one is checked by what reaches the server -- the query string -- rather
 * than by what the box shows, because a filter that draws correctly and asks
 * for the wrong rows is exactly the kind of bug a screenshot does not catch.
 * Six currencies, so the "+" has two to hold; two accounts, so nothing passes
 * because there was only one of something.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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
import { amountLookup } from "../lib/money";
import { presetRanges, stepRange } from "../components/DateRange";
import { CURRENCY_CHIPS, Register } from "./Register";
import type { Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;
const CURRENCIES = ["EUR", "GBP", "USD", "JPY", "CHF", "SEK"];
//: Two groups of two, so a group tick and a lone tick can be told apart.
const CATEGORIES = [
  {
    id: "grp-everyday",
    name: "Everyday",
    categories: [
      { id: "cat-groceries", name: "Groceries" },
      { id: "cat-transport", name: "Transport" },
    ],
  },
  {
    id: "grp-bills",
    name: "Bills",
    categories: [
      { id: "cat-rent", name: "Rent" },
      { id: "cat-power", name: "Electricity" },
    ],
  },
];
//: What the badge beside Needs a category asks for, and what it is told.
let counted: URLSearchParams[] = [];
const BACKLOG = 7;

let asked: URLSearchParams[] = [];

beforeEach(() => {
  // The register remembers its filters (#143); each test starts from none.
  window.localStorage.clear();
  asked = [];
  counted = [];
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    // The badge's count beside Needs a category (#188) asks for one row;
    // it is not the register's request.
    if (path.includes("/transactions?") && path.includes("limit=1")) {
      counted.push(new URLSearchParams(path.split("?")[1]));
      return Promise.resolve({ transactions: [], total: BACKLOG, has_running_balance: false, capped: false });
    }
    if (path.endsWith("/categories")) return Promise.resolve(CATEGORIES);
    if (path.includes("/transactions?") && !path.includes("limit=1")) {
      asked.push(new URLSearchParams(path.split("?")[1]));
      return Promise.resolve({ transactions: [], total: 0, has_running_balance: false, capped: false });
    }
    if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: CURRENCIES });
    if (path.endsWith("/accounts"))
      return Promise.resolve([
        { id: "acc-eur", name: "Current", currency: "EUR", type: "checking", country: null },
        { id: "acc-gbp", name: "Savings", currency: "GBP", type: "savings", country: null },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

const last = () => asked[asked.length - 1];

// --------------------------------------------------------------------------- //
// The amount lookup
// --------------------------------------------------------------------------- //

describe("the amount lookup's text", () => {
  it.each([
    ["45.20", "45.20"],
    ["45,20", "45.20"],
    ["-45.20", "45.20"],
    ["(45,20)", "45.20"],
    ["€ 1.234,56", "1234.56"],
    ["1,234.56", "1234.56"],
    ["45", "45"],
    ["45.", "45"],
    [",5", "0.5"],
  ])("reads %s as %s", (typed, sent) => {
    expect(amountLookup(typed)).toBe(sent);
  });

  it.each(["", "   ", "abc", "1.2.3,4,5"])("reads %j as no amount", (typed) => {
    expect(amountLookup(typed)).toBeNull();
  });
});

describe("the register's filters", () => {
  it("asks for one amount across every money column, signless and normalised", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    fireEvent.change(screen.getByPlaceholderText("in or out, any currency"), {
      target: { value: "-45,20" },
    });
    await waitFor(() => expect(last().get("amount")).toBe("45.20"), { timeout: 2000 });
  });

  it("asks for nothing when the amount is not an amount, and says so", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    const box = screen.getByPlaceholderText("in or out, any currency");
    fireEvent.change(box, { target: { value: "abc" } });
    expect(box.getAttribute("aria-invalid")).toBe("true");
    await new Promise((done) => setTimeout(done, 400));
    expect(asked.every((one) => one.get("amount") === null)).toBe(true);
  });

  it("filters by the Cleared column and by the Source column", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    fireEvent.change(screen.getByRole("combobox", { name: "Cleared" }), {
      target: { value: "reconciled" },
    });
    await waitFor(() => expect(last().get("cleared")).toBe("reconciled"));
    fireEvent.change(screen.getByRole("combobox", { name: "Source" }), {
      target: { value: "transfer" },
    });
    await waitFor(() => expect(last().get("source")).toBe("transfer"));
    expect(last().get("cleared")).toBe("reconciled");

    // Clear filters takes both back off.
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(last().get("source")).toBeNull());
    expect(last().get("cleared")).toBeNull();
  });

  it("offers no 'Latest 3 months' here, and still offers it elsewhere", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    const presets = screen.getByRole("group", { name: "Date range" });
    expect(within(presets).queryByRole("button", { name: "Latest 3 months" })).toBeNull();
    expect(within(presets).getByRole("button", { name: "Latest 6 months" })).toBeTruthy();
    // The report and the receipts screen keep it: the preset list itself is unchanged.
    expect(presetRanges().map((one) => one.label)).toContain("Latest 3 months");
  });
});

// --------------------------------------------------------------------------- //
// More than four currencies
// --------------------------------------------------------------------------- //

describe("the currency chips", () => {
  it("shows the first four and puts the rest under a +", async () => {
    mount();
    const group = await screen.findByRole("group", { name: "Currencies shown" });
    await waitFor(() => expect(within(group).getByRole("button", { name: "EUR" })).toBeTruthy());

    const pressed = within(group)
      .getAllByRole("button")
      .filter((one) => one.hasAttribute("aria-pressed"))
      .map((one) => one.textContent);
    expect(pressed).toEqual(CURRENCIES.slice(0, CURRENCY_CHIPS));

    const more = within(group).getByRole("button", { name: /2 more currencies: CHF, SEK/ });
    expect(more.textContent).toBe("+2");
    expect(more.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "CHF" })).toBeNull();

    fireEvent.click(more);
    const tray = screen.getByRole("group", { name: "More currencies" });
    const chf = within(tray).getByRole("button", { name: "CHF" });
    expect(chf.getAttribute("aria-pressed")).toBe("true");

    // Turning one off from the tray hides it, exactly like a chip on the line.
    fireEvent.click(chf);
    expect(within(tray).getByRole("button", { name: "CHF" }).getAttribute("aria-pressed")).toBe(
      "false",
    );

    // Escape closes the tray.
    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(screen.queryByRole("group", { name: "More currencies" })).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// The date range, by tapping and by keyboard
// --------------------------------------------------------------------------- //

describe("the date range", () => {
  it("steps a window by its own length", () => {
    expect(stepRange({ since: "2026-09-01", until: "2026-09-30" }, -1)).toEqual({
      since: "2026-08-01",
      until: "2026-08-31",
    });
    expect(stepRange({ since: "2026-04-01", until: "2026-09-30" }, 1)).toEqual({
      since: "2026-10-01",
      until: "2027-03-31",
    });
    expect(stepRange({ since: "2026-01-01", until: "2026-12-31" }, -1)).toEqual({
      since: "2025-01-01",
      until: "2025-12-31",
    });
    // February's last day comes out right, leap year or not.
    expect(stepRange({ since: "2024-03-01", until: "2024-03-31" }, -1)?.until).toBe("2024-02-29");
    expect(stepRange({ since: "2026-01-01", until: null }, -1)).toBeNull();
  });

  it("taps through to the month before, and asks the server for it", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    const earlier = screen.getByRole("button", { name: /^Earlier/ });
    expect((earlier as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "This month" }));
    await waitFor(() => expect(last().get("since")).not.toBeNull());
    const expected = stepRange({ since: last().get("since"), until: last().get("until") }, -1)!;
    expect(expected.since).not.toBe(last().get("since"));

    fireEvent.click(earlier);
    await waitFor(() => expect(last().get("since")).toBe(expected.since));
    expect(last().get("until")).toBe(expected.until);
  });

  it("moves along the chips with the arrow keys, and keeps every one in the tab order", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    const presets = screen.getByRole("group", { name: "Date range" });
    const chips = within(presets).getAllByRole("button");
    for (const chip of chips) expect(chip.tabIndex).toBe(0);

    chips[0].focus();
    fireEvent.keyDown(chips[0], { key: "ArrowRight" });
    expect(document.activeElement).toBe(chips[1]);
    fireEvent.keyDown(chips[1], { key: "End" });
    // Past the presets to the last enabled control in the date bar.
    expect(document.activeElement?.textContent).not.toBe(chips[1].textContent);
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(document.activeElement).toBe(chips[0]);
  });
});

// --------------------------------------------------------------------------- //
// The category picker (#188)
// --------------------------------------------------------------------------- //

describe("the category picker", () => {
  const open = async () => {
    fireEvent.click(await screen.findByRole("button", { name: /All categories/ }));
    return screen.getByRole("group", { name: "All categories" });
  };
  const tick = (pop: HTMLElement, name: string) =>
    fireEvent.click(within(pop).getByRole("checkbox", { name: new RegExp(name) }));

  it("replaces the Needs a category checkbox", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    // Untouched is no filter at all.
    expect(last().get("uncategorised")).toBeNull();
    expect(last().getAll("category_id")).toEqual([]);
    expect(last().get("categorised")).toBeNull();
    const pop = await open();
    // The backlog is the first tick, above every group.
    const boxes = within(pop).getAllByRole("checkbox");
    expect(boxes[0].closest("label")?.textContent).toMatch(/^Needs a category/);
  });

  it("asks for the backlog alone after Select none, oldest first", async () => {
    mount();
    const pop = await open();
    fireEvent.click(within(pop).getByRole("button", { name: "Select none" }));
    await waitFor(() => expect(last().get("uncategorised")).toBe("true"));
    expect(last().getAll("category_id")).toEqual([]);
    expect(last().get("categorised")).toBeNull();
    expect(last().get("direction")).toBe("asc");
    expect(screen.getByRole("button", { name: /^Needs a category/ })).toBeTruthy();
  });

  it("adds a category to the backlog rather than narrowing it", async () => {
    mount();
    const pop = await open();
    fireEvent.click(within(pop).getByRole("button", { name: "Select none" }));
    await waitFor(() => expect(last().get("direction")).toBe("asc"));
    tick(pop, "Groceries");
    await waitFor(() => expect(last().getAll("category_id")).toEqual(["cat-groceries"]));
    expect(last().get("uncategorised")).toBe("true");
    expect(screen.getByRole("button", { name: /2 of 5/ })).toBeTruthy();
  });

  it("ticks a whole group from its heading", async () => {
    mount();
    const pop = await open();
    fireEvent.click(within(pop).getByRole("button", { name: "Select none" }));
    tick(pop, "Needs a category");
    tick(pop, "Bills");
    await waitFor(() => expect(last().getAll("category_id").sort()).toEqual(["cat-power", "cat-rent"]));
    expect(last().get("uncategorised")).toBeNull();
  });

  it("asks for every categorised row, not every id, when only the backlog is unticked", async () => {
    mount();
    const pop = await open();
    tick(pop, "Needs a category");
    await waitFor(() => expect(last().get("categorised")).toBe("true"));
    expect(last().getAll("category_id")).toEqual([]);
    expect(last().get("uncategorised")).toBeNull();
    // Leaving the backlog is not a reason to change the order.
    expect(last().get("direction")).toBe("desc");
  });

  it("asks for nothing with nothing ticked, and says so", async () => {
    mount();
    const pop = await open();
    fireEvent.click(within(pop).getByRole("button", { name: "Select none" }));
    await waitFor(() => expect(last().get("uncategorised")).toBe("true"));
    const before = asked.length;
    tick(pop, "Needs a category");
    expect(await screen.findByText(/No categories are ticked/)).toBeTruthy();
    expect(asked.length).toBe(before);
  });

  it("shows how many rows need a category, ticked or not, under the other filters", async () => {
    mount();
    const pop = await open();
    await waitFor(() =>
      expect(within(pop).getByText("Needs a category").closest("label")?.textContent).toContain(
        String(BACKLOG),
      ),
    );
    tick(pop, "Groceries");
    fireEvent.change(screen.getByPlaceholderText("in or out, any currency"), {
      target: { value: "12" },
    });
    await waitFor(() => expect(counted.at(-1)?.get("amount")).toBe("12"), { timeout: 2000 });
    const count = counted.at(-1)!;
    expect(count.get("uncategorised")).toBe("true");
    expect(count.getAll("category_id")).toEqual([]);
    expect(count.get("limit")).toBe("1");
  });
});
