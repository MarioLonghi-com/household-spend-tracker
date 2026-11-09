// @vitest-environment jsdom

/**
 * A fetch of exchange rates over a long range, as any member sees it (#409):
 * the year in flight, how far it has got and who started it, the page asking
 * again until it ends, and where a failed one stopped. Two currencies, and
 * the fetch started by somebody other than whoever is looking.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { ExchangeRates } from "./ExchangeRates";
import type { FxFetch, FxStatus } from "../lib/fx";
import type { Household } from "../lib/types";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const HOUSE = { id: "h1", name: "Home", base_currency: "EUR" } as Household;

function fetchRow(over: Partial<FxFetch> = {}): FxFetch {
  return {
    id: "f1",
    source: "ecb",
    mode: "missing",
    requested_since: "2015-03-02",
    requested_until: "2026-11-09",
    currencies: ["GBP", "SEK"],
    started_at: "2026-11-09T10:00:00Z",
    finished_at: null,
    outcome: "running",
    rates_added: 1500,
    rates_unchanged: 0,
    error: null,
    started_by: "Bob",
    duration_ms: null,
    chunks_total: 12,
    chunks_done: 3,
    chunk_since: "2018-01-01",
    chunk_until: "2018-12-31",
    ...over,
  };
}

function status(over: Partial<FxStatus> = {}): FxStatus {
  return {
    base_currency: "EUR",
    chain: ["manual", "ecb"],
    stale_days: 7,
    monthly_policy: "month_avg",
    show_own_rates: true,
    tiles: {
      last_fetch: null,
      as_of: null,
      stale: false,
      covered: 0,
      total: 2,
      missing: [],
      gaps: 0,
      manual_count: 0,
      manual_changed_at: null,
      manual_changed_by: null,
    },
    running: false,
    running_fetches: [],
    would_fetch: [{ source: "ecb", mode: "missing", currencies: ["GBP", "SEK"], since: "2015-03-02", until: "2026-11-09" }],
    would_fetch_refused: null,
    would_fetch_reason: null,
    currencies: [],
    not_covered: [],
    history: [],
    rates_status: {
      as_of: null,
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
    ...over,
  };
}

function mount(answers: FxStatus[]) {
  let call = 0;
  vi.mocked(api.get).mockImplementation((path: string) => {
    if (path.endsWith("/fx/status")) return Promise.resolve(answers[Math.min(call++, answers.length - 1)]);
    return Promise.resolve({ base_currency: "EUR", total: 0, limit: 500, rates: [] });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ExchangeRates household={HOUSE} />
    </QueryClientProvider>,
  );
}

function historyCells(): string[] {
  const section = screen.getByRole("heading", { name: "Fetch history" }).closest("section")!;
  const [, row] = within(section).getAllByRole("row");
  return within(row).getAllByRole("cell").map((cell) => cell.textContent ?? "");
}

describe("a fetch over a long range", () => {
  it("shows a running fetch to whoever opens the page, then asks again until it ends", async () => {
    const running = fetchRow();
    const done = fetchRow({ outcome: "ok", chunks_done: 12, chunk_since: null, chunk_until: null, finished_at: "2026-11-09T10:02:00Z", duration_ms: 120_000 });
    mount([
      status({ running: true, running_fetches: [running], history: [running] }),
      status({ history: [done] }),
    ]);

    const progress = await screen.findByText(/Fetching GBP SEK from ECB: 2018-01-01 to 2018-12-31, 4 of 12, of 2015-03-02 to 2026-11-09\./);
    const banner = progress.closest("[role=status]")!;
    expect(banner.textContent).toContain("Started by Bob");
    expect(banner.textContent).toContain("It carries on if they leave the page");
    const bar = within(banner as HTMLElement).getByRole("progressbar", { name: "Fetch progress" }) as HTMLProgressElement;
    expect([bar.value, bar.max]).toEqual([3, 12]);
    expect(historyCells().at(-1)).toBe("Running · 4 of 12");
    expect((screen.getByRole("button", { name: "Fetch now" }) as HTMLButtonElement).disabled).toBe(true);

    // The page polls while a fetch runs; the next answer says it has ended.
    await waitFor(() => expect(screen.queryByText(/Fetching GBP SEK/)).toBeNull(), { timeout: 3000 });
    expect(historyCells().at(-1)).toBe("Done");
    expect((screen.getByRole("button", { name: "Fetch now" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("says in the history where a failed fetch stopped", async () => {
    const failed = fetchRow({
      outcome: "failed",
      chunks_done: 2,
      chunk_since: "2017-01-01",
      chunk_until: "2017-12-31",
      error: "could not reach data-api.ecb.europa.eu: no route to host",
      finished_at: "2026-11-09T10:01:00Z",
      duration_ms: 60_000,
    });
    mount([status({ history: [failed] })]);
    await screen.findByRole("heading", { name: "Fetch history" });
    await waitFor(() => expect(historyCells().length).toBeGreaterThan(0));
    expect(historyCells().at(-1)).toBe(
      "Failedcould not reach data-api.ecb.europa.eu: no route to hostStopped at 3 of 12, 2017-01-01 to 2017-12-31.",
    );
  });
});
