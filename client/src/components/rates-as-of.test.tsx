// @vitest-environment jsdom

/**
 * The "Rates as of" line in each of its states (#396 §5.2, §10).
 *
 * What is asserted is the sentence a person reads and whether it is drawn as a
 * warning, from a `rates_status` block like the one a converting report sends:
 * two currencies, so "the weakest link sets the date" has two links.
 */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RatesAsOf, ratesState } from "./RatesAsOf";
import type { RatesStatus } from "../lib/fx";

afterEach(cleanup);

function status(over: Partial<RatesStatus> = {}): RatesStatus {
  return {
    as_of: "2026-11-06",
    stale: false,
    running: false,
    fetched_at: "2026-11-09T14:02:11",
    fetched_by: "Robin",
    last_fetch_failed: null,
    chain: ["manual", "ecb"],
    stale_days: 7,
    remote_in_chain: true,
    manual_changed_at: null,
    manual_changed_by: null,
    per_currency: [
      { currency: "GBP", rate_date: "2026-11-07", source: "ecb", stale: false, age_days: 2 },
      { currency: "SEK", rate_date: "2026-11-06", source: "ecb", stale: false, age_days: 3 },
    ],
    not_converted: [],
    ...over,
  };
}

function line(): HTMLElement {
  return screen.getByRole("status");
}

describe("RatesAsOf", () => {
  it("fresh: the oldest rate date, the source, and who fetched when", () => {
    render(<RatesAsOf status={status()} />);
    expect(line().textContent).toContain("Rates as of 2026-11-06 · ECB · fetched");
    expect(line().textContent).toContain("by Robin");
    expect(line().className).not.toContain("warn");
  });

  it("stale: the age in days of the oldest, as a warning", () => {
    const old = status({
      stale: true,
      as_of: "2026-10-24",
      per_currency: [{ currency: "GBP", rate_date: "2026-10-24", source: "ecb", stale: true, age_days: 16 }],
    });
    render(<RatesAsOf status={old} />);
    expect(line().textContent).toContain("Rates as of 2026-10-24, 16 days old · fetch them on the Exchange rates page");
    expect(line().className).toContain("warn");
  });

  it("running: says so and nothing else", () => {
    render(<RatesAsOf status={status({ running: true })} />);
    expect(line().textContent).toBe("Fetching rates…?");
  });

  it("failed: the date it still has and why the last fetch failed", () => {
    render(
      <RatesAsOf
        status={status({ last_fetch_failed: { at: "2026-11-09T14:02:11", error: "could not reach the ECB" } })}
      />,
    );
    expect(line().textContent).toContain("Rates as of 2026-11-06 · the last fetch failed on");
    expect(line().textContent).toContain(": could not reach the ECB");
    expect(line().className).toContain("warn");
  });

  it("never fetched: no rates yet, and where to fetch them", () => {
    render(<RatesAsOf status={status({ as_of: null, per_currency: [], not_converted: ["GBP", "SEK"] })} />);
    expect(line().textContent).toContain("No exchange rates yet · fetch them on the Exchange rates page");
    expect(line().textContent).toContain("GBP, SEK not converted");
  });

  it("manual only: entered by hand, and the last change", () => {
    const manual = status({
      fetched_at: null,
      fetched_by: null,
      manual_changed_at: "2026-11-02T09:00:00",
      manual_changed_by: "Sam",
      per_currency: [{ currency: "BRL", rate_date: "2026-10-31", source: "manual", stale: false, age_days: 9 }],
      as_of: "2026-10-31",
    });
    render(<RatesAsOf status={manual} />);
    expect(line().textContent).toContain("Rates as of 2026-10-31 · entered by hand · last change");
    expect(line().textContent).toContain("by Sam");
    expect(line().className).not.toContain("warn");
  });

  it("a currency with no rate is named, as a warning", () => {
    render(<RatesAsOf status={status({ not_converted: ["ARS"] })} />);
    expect(line().textContent).toContain("· ARS not converted");
    expect(line().className).toContain("warn");
  });

  it("hidden when nothing on the page was converted: one currency", () => {
    const { container } = render(<RatesAsOf status={status({ as_of: null, per_currency: [], not_converted: [] })} />);
    expect(container.innerHTML).toBe("");
    expect(ratesState(status({ per_currency: [], not_converted: [] }))).toBe("hidden");
  });

  it("the ? lists each currency with its own date and source, and the link opens the page", () => {
    const onOpen = vi.fn();
    render(<RatesAsOf status={status()} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: /What's this\?/ }));
    const note = screen.getByRole("note");
    expect(note.textContent).toContain("GBP: 2026-11-07 · ECB");
    expect(note.textContent).toContain("SEK: 2026-11-06 · ECB");
    fireEvent.click(screen.getByRole("button", { name: "Exchange rates" }));
    expect(onOpen).toHaveBeenCalledOnce();
  });
});
