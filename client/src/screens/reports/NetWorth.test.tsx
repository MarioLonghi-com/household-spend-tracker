// @vitest-environment jsdom

/**
 * The Net Worth report (#397), handed fixtures.
 *
 * Every assertion is a value on screen or in the address: the exact total in
 * one currency and no "≈"; two currencies with no rates drawn side by side and
 * never added; a converted answer wearing "≈" with its exchange-rate effect
 * and the worked conversion line; the accounts table's order after a heading
 * is clicked; where an account's name sends somebody; and the filters written
 * into the address and read back from it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
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

import { api } from "../../lib/api";
import { NetWorth, partSlots, splitLabeller, ticks } from "./NetWorth";
import {
  UNSET_ID,
  addressFor,
  choosesNothing,
  defaultView,
  reportQuery,
  viewFromSearch,
} from "./netWorthView";
import { REPORTS } from "../Reports";
import { format } from "../../lib/money";
import type { Account, Household, NetWorth as Report, NetWorthBook } from "../../lib/types";
import type { RatesStatus } from "../../lib/fx";

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function account(over: Partial<Account>): Account {
  return {
    id: "a", name: "A", type: "checking", currency: "EUR", closed: false, note: null,
    institution: null, country: null, flag: "", is_liability: false, sort_order: 0,
    statement_product: null, balance: 0, cleared: 0, uncleared: 0, transaction_count: 0,
    oldest_transaction: null, newest_transaction: null, opening_balance: 0, opening_date: null,
    opening_transaction_id: null, warnings: [],
    ...over,
  };
}

const ACCOUNTS = [
  account({ id: "santander", name: "Santander", institution: "Banco Santander", country: "ES" }),
  account({ id: "visa", name: "Visa", type: "credit_card", is_liability: true, institution: "Banco Santander", country: "ES" }),
  account({ id: "monzo", name: "Monzo", type: "savings", currency: "GBP", institution: "Monzo", country: "GB" }),
  account({ id: "pocket", name: "Pocket", type: "cash", currency: "GBP" }),
];

function book(currency: string, start: number, end: number, accounts: number): NetWorthBook {
  return {
    currency,
    accounts,
    points: [
      { date: "2026-09-30", today: false, net_worth: start, liabilities: 0, parts: { [currency]: start }, accounts_without_data: 0, stale_rate: false, missing_rates: [] },
      { date: "2026-10-31", today: false, net_worth: end, liabilities: 0, parts: { [currency]: end }, accounts_without_data: 0, stale_rate: false, missing_rates: [] },
    ],
    headline: { net_worth: end, liabilities: 0, change_prev: end - start, prev_date: "2026-09-30", change_range: end - start, range_start: "2026-09-30" },
    bridge: { start, fx_effect: 0, money_added_or_spent: end - start, end },
    held: [{ key: currency, native_minor: end, native_currency: currency, rate: null, rate_date: null, value: end, share: "1.0000" }],
  };
}

function base(over: Partial<Report> = {}): Report {
  return {
    as_of: "2026-10-31", since: "2026-09-01", until: "2026-11-30", today: "2026-11-09",
    closed_only: true, clipped_from: "2026-11-09", split: "currency", base_currency: "EUR",
    display_currency: null, converted: false, rate_source: null, rates_status: null,
    points: [], headline: null, bridge: null, held: [], accounts: [], not_converted: [],
    currencies: [],
    explanation: {
      accounts_counted: 4, accounts_hidden_closed: 0, accounts_without_data: 0,
      accounts_without_data_at_start: 0, currencies: ["EUR", "GBP"], rates: [],
      largest_foreign: null, stale: false, missing: [],
    },
    ...over,
  };
}

/** One currency in scope: exact, no rate, the figures of the EUR book. */
function oneCurrency(): Report {
  const eur = book("EUR", 400000, 375000, 2);
  return base({
    display_currency: "EUR",
    points: eur.points, headline: eur.headline, bridge: eur.bridge, held: eur.held,
    currencies: [eur],
    accounts: [
      { id: "visa", name: "Visa", institution: "Banco Santander", country: "ES", type: "credit_card", is_liability: true, closed: false, currency: "EUR", native_minor: -25000, native_change: -25000, value: -25000, change_range: -25000 },
      { id: "santander", name: "Santander", institution: "Banco Santander", country: "ES", type: "checking", is_liability: false, closed: false, currency: "EUR", native_minor: 400000, native_change: 0, value: 400000, change_range: 0 },
    ],
    explanation: { ...base().explanation, accounts_counted: 2, currencies: ["EUR"] },
  });
}

/** Two currencies and no rates: two books, no total. */
function noRates(): Report {
  return base({
    currencies: [book("EUR", 400000, 375000, 2), book("GBP", 90000, 91000, 2)],
    accounts: [
      { id: "santander", name: "Santander", institution: "Banco Santander", country: "ES", type: "checking", is_liability: false, closed: false, currency: "EUR", native_minor: 400000, native_change: 0, value: null, change_range: null },
      { id: "monzo", name: "Monzo", institution: "Monzo", country: "GB", type: "savings", is_liability: false, closed: false, currency: "GBP", native_minor: 90000, native_change: 1000, value: null, change_range: null },
    ],
  });
}

/** §5.6, converted at a fixed table. */
function converted(): Report {
  const points = [
    { date: "2025-10-31", today: false, net_worth: 2000000, liabilities: 0, parts: { EUR: 1000000, GBP: 1000000 }, accounts_without_data: 0, stale_rate: false, missing_rates: [] },
    { date: "2026-10-31", today: false, net_worth: 2141176, liabilities: 0, parts: { EUR: 1200000, GBP: 941176 }, accounts_without_data: 0, stale_rate: false, missing_rates: [] },
  ];
  return base({
    display_currency: "EUR", converted: true,
    rate_source: { chain: ["manual", "ecb"], policy: "spot" },
    rates_status: status(),
    points,
    headline: { net_worth: 2141176, liabilities: 0, change_prev: 141176, prev_date: "2025-10-31", change_range: 141176, range_start: "2025-10-31" },
    bridge: { start: 2000000, fx_effect: -58824, money_added_or_spent: 200000, end: 2141176 },
    held: [
      { key: "EUR", native_minor: 1200000, native_currency: "EUR", rate: null, rate_date: null, value: 1200000, share: "0.5604" },
      { key: "GBP", native_minor: 800000, native_currency: "GBP", rate: "0.85", rate_date: "2026-10-30", value: 941176, share: "0.4396" },
    ],
    currencies: [book("EUR", 1000000, 1200000, 1), book("GBP", 800000, 800000, 1)],
    accounts: [
      { id: "eur", name: "Savings EUR", institution: null, country: null, type: "savings", is_liability: false, closed: false, currency: "EUR", native_minor: 1200000, native_change: 200000, value: 1200000, change_range: 200000 },
      { id: "gbp", name: "Savings GBP", institution: null, country: null, type: "savings", is_liability: false, closed: false, currency: "GBP", native_minor: 800000, native_change: 0, value: 941176, change_range: -58824 },
    ],
    explanation: {
      ...base().explanation, accounts_counted: 2,
      rates: [{ currency: "GBP", rate: "0.85", rate_date: "2026-10-30", source: "manual", stale: false, before_as_of: true }],
      largest_foreign: { account_id: "gbp", account_name: "Savings GBP", currency: "GBP", native_minor: 800000, rate: "0.85", value: 941176 },
    },
  });
}

let asked: URLSearchParams[] = [];
let answer: () => Report;

beforeEach(() => {
  asked = [];
  answer = oneCurrency;
  window.history.replaceState(null, "", "/");
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.includes("/reports/net-worth?")) {
      asked.push(new URLSearchParams(path.split("?")[1]));
      return Promise.resolve(answer());
    }
    if (path.endsWith("/accounts")) return Promise.resolve(ACCOUNTS);
    return Promise.resolve([]);
  }) as typeof api.get);
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function mount(onOpenRegister = vi.fn(), onOpenExchangeRates = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <NetWorth household={HOUSEHOLD} onOpenRegister={onOpenRegister} onOpenExchangeRates={onOpenExchangeRates} />
    </QueryClientProvider>,
  );
  return { onOpenRegister, onOpenExchangeRates, ...view };
}

/** A `rates_status` block, as `fx.rates_status` sends it. */
function status(over: Partial<RatesStatus> = {}): RatesStatus {
  return {
    as_of: "2026-10-30", stale: false, running: false, fetched_at: null, fetched_by: null,
    last_fetch_failed: null, chain: ["manual", "ecb"], stale_days: 7, remote_in_chain: true,
    manual_changed_at: null, manual_changed_by: null,
    per_currency: [{ currency: "GBP", rate_date: "2026-10-30", source: "ecb", stale: false, age_days: 10 }],
    not_converted: [],
    ...over,
  };
}

describe("the choices and the address", () => {
  it("round-trips every filter, with 'not set' sent as an empty value", () => {
    const view = {
      ...defaultView(new Date(2026, 10, 9)),
      closedOnly: false,
      banks: ["Monzo", UNSET_ID],
      countries: ["GB"],
      currencies: null,
      accounts: ["monzo"],
      showClosed: true,
      split: "institution" as const,
    };
    const query = reportQuery(view);
    const sent = new URLSearchParams(query);
    expect(sent.getAll("institution")).toEqual(["Monzo", ""]);
    expect(sent.get("since")).toBe("2025-12-01");
    expect(sent.get("until")).toBe("2026-11-30");
    expect(sent.get("closed_only")).toBe("false");
    expect(sent.has("currency")).toBe(false);

    const address = addressFor(view, "house-1");
    expect(address.startsWith("/?open=report-net-worth&household=house-1&")).toBe(true);
    expect(viewFromSearch(address.slice(1), new Date(2026, 10, 9))).toEqual(view);
  });

  it("opens on the latest 12 months, closed months only, everything", () => {
    const view = viewFromSearch("", new Date(2026, 10, 9));
    expect(view.range).toEqual({ since: "2025-12-01", until: "2026-11-30" });
    expect(view.closedOnly).toBe(true);
    expect(view.banks).toBeNull();
    expect(new URLSearchParams(reportQuery(view)).has("closed_only")).toBe(false);
    expect(viewFromSearch("range=all").range).toEqual({ since: null, until: null });
  });

  it("knows a filter ticked down to nothing asks for nothing", () => {
    expect(choosesNothing({ ...defaultView(), countries: [] })).toBe(true);
    expect(choosesNothing(defaultView())).toBe(false);
  });

  it("is listed on the Reports index after the other two", () => {
    expect(REPORTS.map((one) => one.key)).toEqual(["income-expense", "reimbursements", "net-worth"]);
  });
});

describe("the chart's furniture", () => {
  it("gives each part a colour by the part, so narrowing never repaints the rest", () => {
    const label = splitLabeller("currency", []);
    const all = partSlots(ACCOUNTS, "currency", label);
    const fewer = partSlots(ACCOUNTS, "currency", label);
    expect(all.get("GBP")).toBe(fewer.get("GBP"));
    expect([...all.entries()]).toEqual([["EUR", 1], ["GBP", 2]]);
    // An account with no bank is filed under the empty key, named in words.
    expect(splitLabeller("institution", [])("")).toBe("No bank set");
  });

  it("steps the value axis in round numbers through zero", () => {
    expect(ticks(-25000, 400000)).toEqual([-200000, 0, 200000, 400000]);
    expect(ticks(0, 950)).toEqual([0, 500, 1000]);
    expect(ticks(0, 0)).toEqual([0]);
  });
});

describe("the report as a screen", () => {
  it("in one currency is exact: no '≈', no exchange-rate line, and the total as sent", async () => {
    mount();
    const takeaway = await screen.findByText(/^On 2026-10-31 the household held/);
    expect(takeaway.textContent).toBe(
      `On 2026-10-31 the household held ${format(375000, "EUR")} across 2 accounts, down ${format(25000, "EUR")} on 2026-09-30.`,
    );
    expect(document.body.textContent).not.toContain("≈");
    expect(screen.queryByText("Exchange-rate effect")).toBeNull();
    expect(screen.getByText("Money added or spent")).toBeTruthy();
    expect(screen.getByText("Stops at 2026-10-31, the last closed month: the month still running is left out.")).toBeTruthy();
    expect(asked[0].get("since")).toMatch(/^\d{4}-\d{2}-01$/);
  });

  it("sorts the accounts at their headings and opens one in the register up to the as-of date", async () => {
    const { onOpenRegister } = mount();
    await screen.findByText(/^On 2026-10-31/);
    const table = document.querySelector("table.nw-accounts") as HTMLTableElement;
    const names = () => [...table.querySelectorAll("tbody tr")].map((row) => row.querySelector("td")!.textContent);
    expect(names()).toEqual(["Santander", "Visa"]);
    fireEvent.click(within(table).getByRole("button", { name: /Value in EUR/ }));
    expect(names()).toEqual(["Visa", "Santander"]);
    fireEvent.click(within(table).getByRole("button", { name: "Santander" }));
    expect(onOpenRegister).toHaveBeenCalledWith({ accounts: ["santander"], range: { since: null, until: "2026-10-31" } });
  });

  it("with two currencies and no rates shows each on its own and never a sum", async () => {
    answer = noRates;
    mount();
    expect(await screen.findByRole("heading", { name: "EUR" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "GBP" })).toBeTruthy();
    expect(screen.getByText(/there are no exchange rates to convert between them/)).toBeTruthy();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("≈");
    expect(text).toContain(format(375000, "EUR"));
    expect(text).toContain(format(91000, "GBP"));
    // No column of values in a currency the page does not have.
    expect(screen.queryByRole("button", { name: /Value in/ })).toBeNull();
  });

  it("converted, wears '≈', splits out the exchange-rate effect and shows the worked line", async () => {
    answer = converted;
    mount();
    await screen.findByText(/^On 2026-10-31/);
    expect(screen.getByText("Exchange-rate effect")).toBeTruthy();
    const text = document.body.textContent ?? "";
    expect(text).toContain(`≈ ${format(2141176, "EUR")}`);
    expect(text).toContain(`≈ ${format(-58824, "EUR")}`);
    expect(text).toContain(`${format(800000, "GBP")} ÷ 0.85 = ${format(941176, "EUR")}`);
    expect(text).toContain("the last rate before 2026-10-31");
    expect(text).toContain("GBP: 0.85 per 1 EUR, published 2026-10-30 (entered by hand)");
    expect(text).toContain("Rates come from entered by hand and ECB");
    // The "Rates as of" line, from the same response, under the filter bar.
    expect(screen.getByRole("status").textContent).toContain("Rates as of 2026-10-30");
  });

  it("with no rate stored yet says so on the line and links to the Exchange rates page", async () => {
    answer = () => ({ ...noRates(), rates_status: status({ as_of: null, per_currency: [], not_converted: ["GBP"] }) });
    const { onOpenExchangeRates } = mount();
    expect(await screen.findByText(/No exchange rates yet/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Fetch or enter rates on the Exchange rates page" }));
    expect(onOpenExchangeRates).toHaveBeenCalledTimes(1);
  });

  it("in one currency has no Rates as of line", async () => {
    mount();
    await screen.findByText(/^On 2026-10-31/);
    expect(screen.queryByText(/Rates as of/)).toBeNull();
  });

  it("writes its filters into the address and gives the address back when it closes", async () => {
    const { unmount } = mount();
    await screen.findByText(/^On 2026-10-31/);
    const query = new URLSearchParams(window.location.search);
    expect(query.get("open")).toBe("report-net-worth");
    expect(query.get("household")).toBe("house-1");
    fireEvent.click(screen.getByRole("checkbox", { name: "Closed months only" }));
    expect(new URLSearchParams(window.location.search).get("closed_only")).toBe("false");
    unmount();
    expect(window.location.search).toBe("");
  });

  it("opens on the view the address names", async () => {
    window.history.replaceState(null, "", "/?open=report-net-worth&household=house-1&range=all&split=type&country=");
    mount();
    await screen.findByText(/^On 2026-10-31/);
    const sent = asked[0];
    expect(sent.has("since")).toBe(false);
    expect(sent.get("split")).toBe("type");
    expect(sent.getAll("country")).toEqual([""]);
  });

  it("says so when no account matches, and resets", async () => {
    answer = () => base({ explanation: { ...base().explanation, accounts_counted: 0, currencies: [] } });
    window.history.replaceState(null, "", "/?open=report-net-worth&household=house-1&country=JP");
    mount();
    expect(await screen.findByText("No accounts match these filters.")).toBeTruthy();
    answer = oneCurrency;
    fireEvent.click(screen.getByRole("button", { name: "Reset the filters" }));
    expect(await screen.findByText(/^On 2026-10-31/)).toBeTruthy();
  });

  it("opens the explanation the first time and remembers it shut", async () => {
    mount();
    await screen.findByText(/^On 2026-10-31/);
    const details = document.querySelector("details.nw-explain") as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(details.textContent).toContain("Nothing is converted: every account here is in EUR, so the figures are exact.");
    details.open = false;
    fireEvent(details, new Event("toggle"));
    expect(window.localStorage.getItem("net-worth-explanation-open")).toBe("false");
  });
});
