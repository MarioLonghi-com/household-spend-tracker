// @vitest-environment jsdom

/**
 * Browse rates (#410): a long range comes a page at a time, the filters and
 * the order reach the server, and all of it is in the address so a reload
 * opens the list where it was. Two foreign currencies, as everywhere.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { ExchangeRates } from "./ExchangeRates";
import {
  BROWSE_PAGE,
  browseAddress,
  browseViewFromSearch,
  defaultBrowseView,
  serverQuery,
  type BrowseView,
} from "./exchangeRates/browseView";
import type { FxCurrency, FxRate, FxRates, FxStatus } from "../lib/fx";
import type { Household } from "../lib/types";

const HOUSE = { id: "h1", name: "Home", base_currency: "EUR" } as Household;

function currency(code: string): FxCurrency {
  return {
    currency: code,
    accounts: [{ id: `a-${code}`, name: `${code} account`, institution: null, country: null, country_name: null, flag: "", closed: false }],
    source_in_use: "ecb",
    first_rate: "2016-01-04",
    last_rate: "2026-11-04",
    rates: 2800,
    age_days: 5,
    transaction_dates: 2,
    coverage_percent: 100,
    gaps: 0,
    status: "ok",
    own_rates: 0,
    iso: true,
    months: [],
  };
}

const STATUS = {
  base_currency: "EUR",
  chain: ["manual", "ecb"],
  stale_days: 7,
  monthly_policy: "month_avg",
  show_own_rates: true,
  tiles: {
    last_fetch: null,
    as_of: "2026-11-04",
    stale: false,
    covered: 2,
    total: 2,
    missing: [],
    gaps: 0,
    manual_count: 0,
    manual_changed_at: null,
    manual_changed_by: null,
  },
  running: false,
  running_fetches: [],
  would_fetch: [],
  would_fetch_refused: null,
  would_fetch_reason: null,
  currencies: [currency("GBP"), currency("SEK")],
  not_covered: [],
  history: [],
  rates_status: {
    as_of: "2026-11-04",
    stale: false,
    running: false,
    fetched_at: null,
    fetched_by: null,
    last_fetch_failed: null,
    chain: ["manual", "ecb"],
    stale_days: 7,
    remote_in_chain: true,
    manual_changed_at: null,
    manual_changed_by: null,
    per_currency: [],
    not_converted: [],
  },
} satisfies FxStatus;

/** Ten years of two currencies' weekday rates, as the server counts them. */
const TOTAL = 5_600;

/** One page as the server would answer it: `count` rows from `offset`. */
function page(offset: number, count = BROWSE_PAGE, focus: number | null = null): FxRates {
  const rates: FxRate[] = Array.from({ length: count }, (_, i) => {
    const at = offset + i;
    const day = new Date(Date.UTC(2026, 10, 4) - Math.floor(at / 2) * 86_400_000).toISOString().slice(0, 10);
    return {
      id: null,
      source: "ecb",
      quote_currency: "EUR",
      currency: at % 2 ? "GBP" : "SEK",
      date: day,
      period: "day",
      rate: `1.${at}`,
      rate_in_base: `1.${at}`,
      own_rate: null,
    };
  });
  return { base_currency: "EUR", granularity: "day", total: TOTAL, limit: BROWSE_PAGE, offset, focus, rates };
}

/** The `fx/rates` requests the list made, as parsed query strings. */
function asked(): URLSearchParams[] {
  return vi
    .mocked(api.get)
    .mock.calls.map(([path]) => path as string)
    .filter((path) => path.includes("/fx/rates?") && !path.includes("source=manual&limit=5000"))
    .map((path) => new URLSearchParams(path.split("?")[1]));
}

function mount(answer: (query: URLSearchParams) => FxRates = (q) => page(Number(q.get("offset") ?? 0))) {
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path.endsWith("/fx/status")) return Promise.resolve(STATUS);
    if (path.includes("source=manual&limit=5000")) {
      return Promise.resolve({ base_currency: "EUR", total: 0, limit: 5000, rates: [] });
    }
    return Promise.resolve(answer(new URLSearchParams(path.split("?")[1])));
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ExchangeRates household={HOUSE} />
    </QueryClientProvider>,
  );
}

function browseCard(): HTMLElement {
  return screen.getByRole("heading", { name: "Browse rates" }).closest("section")!;
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  // jsdom draws nothing, so it has no scrolling; the page asks for some.
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("the Browse rates view and its address", () => {
  it("writes every choice into the address and reads it back unchanged", () => {
    const view: BrowseView = {
      currency: "GBP",
      source: "ecb",
      granularity: "month_end",
      range: { since: "2020-01-01", until: "2024-12-31" },
      sort: "in_base",
      direction: "asc",
      page: 7,
      at: null,
    };
    const address = browseAddress(view, "h1");
    expect(address.startsWith("/?open=exchange-rates&household=h1&")).toBe(true);
    expect(browseViewFromSearch(address.slice(1))).toEqual(view);

    const jumped = { ...view, at: "2022-06-01" };
    expect(browseViewFromSearch(browseAddress(jumped, "h1").slice(1))).toEqual({ ...jumped, page: 1 });
  });

  it("leaves what it cannot read at its default, and a plain visit has a plain address", () => {
    expect(browseViewFromSearch("?currency=../me&sort=rate;drop&page=-3&since=yesterday&granularity=week")).toEqual(
      defaultBrowseView(),
    );
    expect(browseAddress(defaultBrowseView(), "h1")).toBe("/?open=exchange-rates&household=h1");
  });

  it("asks the server for one page by offset, or for the page holding a date", () => {
    const query = new URLSearchParams(serverQuery({ ...defaultBrowseView(), page: 3 }));
    expect([query.get("limit"), query.get("offset"), query.get("page")]).toEqual([String(BROWSE_PAGE), "200", null]);
    const jump = new URLSearchParams(serverQuery({ ...defaultBrowseView(), page: 3, at: "2020-02-03" }));
    expect([jump.get("at"), jump.get("offset")]).toEqual(["2020-02-03", null]);
  });
});

describe("Browse rates on the Exchange rates page", () => {
  it("draws one page of thousands of rates, and the next only when asked", async () => {
    mount();
    const card = await waitFor(() => browseCard());
    await within(card).findByText(`1–100 of 5,600 rates`);
    // A heading row and a hundred rows: not five thousand six hundred.
    expect(within(card).getAllByRole("row")).toHaveLength(BROWSE_PAGE + 1);
    expect(asked().at(-1)!.get("offset")).toBe("0");

    fireEvent.click(within(card).getAllByRole("button", { name: "Next page" })[0]);
    await within(card).findByText(`101–200 of 5,600 rates`);
    expect(within(card).getAllByRole("row")).toHaveLength(BROWSE_PAGE + 1);
    expect(asked().at(-1)!.get("offset")).toBe("100");
    expect(new URLSearchParams(window.location.search).get("page")).toBe("2");

    fireEvent.click(within(card).getAllByRole("button", { name: "Last page" })[0]);
    await within(card).findByText(`5,501–5,600 of 5,600 rates`);
    expect(asked().at(-1)!.get("offset")).toBe("5500");
  });

  it("opens on the filters, order and page the address holds, and asks the server for them", async () => {
    window.history.replaceState(
      null,
      "",
      "/?open=exchange-rates&household=h1&currency=GBP&granularity=month_avg&since=2020-01-01&until=2024-12-31&sort=in_base&direction=asc&page=3",
    );
    mount();
    const card = await waitFor(() => browseCard());
    await waitFor(() => expect(asked().length).toBeGreaterThan(0));
    const first = asked()[0];
    expect(Object.fromEntries(first)).toEqual({
      currency: "GBP",
      granularity: "month_avg",
      since: "2020-01-01",
      until: "2024-12-31",
      sort: "in_base",
      direction: "asc",
      limit: String(BROWSE_PAGE),
      offset: "200",
    });
    expect((within(card).getByLabelText("Currency") as HTMLSelectElement).value).toBe("GBP");
    expect((within(card).getByLabelText("Show") as HTMLSelectElement).value).toBe("month_avg");
    // By month there is no "As published"; the rate's own date is there instead.
    await within(card).findAllByText("1.200");
    expect(within(card).queryByRole("button", { name: "As published" })).toBeNull();
    expect(within(card).getByRole("button", { name: "Rate's date" })).toBeTruthy();

    // A new filter goes to the server and to the address, from the first page.
    fireEvent.change(within(card).getByLabelText("Currency"), { target: { value: "SEK" } });
    await waitFor(() => expect(asked().at(-1)!.get("currency")).toBe("SEK"));
    expect(asked().at(-1)!.get("offset")).toBe("0");
    const address = new URLSearchParams(window.location.search);
    expect([address.get("open"), address.get("currency"), address.get("page"), address.get("granularity")]).toEqual([
      "exchange-rates",
      "SEK",
      null,
      "month_avg",
    ]);
  });

  it("sorts every column through the server, at its heading", async () => {
    mount();
    const card = await waitFor(() => browseCard());
    await within(card).findByText(`1–100 of 5,600 rates`);
    for (const [name, column] of [
      ["Currency", "currency"],
      ["Source", "source"],
      ["As published", "rate"],
      ["Per 1 EUR", "in_base"],
      ["Your transfers", "own"],
    ] as const) {
      fireEvent.click(within(card).getByRole("button", { name }));
      await waitFor(() => expect(asked().at(-1)!.get("sort")).toBe(column));
      expect(asked().at(-1)!.get("direction")).toBe("asc");
    }
    fireEvent.click(within(card).getByRole("button", { name: "Date" }));
    await waitFor(() => expect(asked().at(-1)!.get("sort")).toBeNull());
    expect(new URLSearchParams(window.location.search).get("sort")).toBeNull();
  });

  it("jumps to a date: asks for the page holding it and marks the row it landed on", async () => {
    mount((q) => (q.get("at") ? page(1_200, BROWSE_PAGE, 7) : page(Number(q.get("offset") ?? 0))));
    const card = await waitFor(() => browseCard());
    await within(card).findByText(`1–100 of 5,600 rates`);
    fireEvent.change(within(card).getByLabelText("Jump to date"), { target: { value: "2025-03-14" } });
    fireEvent.click(within(card).getByRole("button", { name: "Go" }));
    await within(card).findByText(`1,201–1,300 of 5,600 rates`);
    expect(asked().at(-1)!.get("at")).toBe("2025-03-14");
    const landed = within(card)
      .getAllByRole("row")
      .find((row) => row.getAttribute("aria-current") === "true")!;
    expect(within(landed).getAllByText("1.1207")).toHaveLength(2);
    expect(within(card).getAllByText("Page 13 of 56")).toHaveLength(2);
  });

  it("a currency's Browse rates button filters the list to it", async () => {
    mount();
    const perCurrency = (await screen.findByRole("heading", { name: "Per currency" })).closest("section")!;
    const sek = within(perCurrency)
      .getAllByRole("row")
      .find((row) => row.textContent?.startsWith("SEK"))!;
    fireEvent.click(within(sek).getByRole("button", { name: "Browse rates" }));
    await waitFor(() => expect(asked().at(-1)!.get("currency")).toBe("SEK"));
    expect(new URLSearchParams(window.location.search).get("currency")).toBe("SEK");
  });
});
