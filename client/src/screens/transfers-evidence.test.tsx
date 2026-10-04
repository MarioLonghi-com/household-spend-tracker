// @vitest-environment jsdom

/**
 * What the Transfers screen shows about the evidence behind a pair (#127,
 * #131).
 *
 * "Worth a look" puts the pairs whose two descriptions say the same thing
 * first, whatever order the server sent them in. Any offered pair can be
 * marked "Not a transfer", and links made on account history alone are
 * listed with Unlink and Keep.
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
import type { Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as Household;

function leg(id: string, account: string, date: string, amount: number) {
  return { id, account_id: `acct-${account}`, account_name: account, date, amount, currency: "EUR", description: `words ${id}` };
}

function pair(out: string, into: string, why: string, words: string[] = []) {
  return {
    out_leg: leg(out, "Current", "2026-04-01", -100),
    in_leg: leg(into, "Saver", "2026-04-02", 100),
    strength: "suggested",
    why,
    words,
  };
}

const FINDINGS = {
  strong: [],
  suggested: [
    pair("o1", "i1", "the amounts match and the dates are close"),
    pair("o2", "i2", "the amounts match and the dates are close; both rows say ZED", ["ZED"]),
    pair("o3", "i3", "a household member's name is on it, and the amounts match"),
  ],
  awaiting: [],
};

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Transfers household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

describe("Worth a look", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) => (url.endsWith("/categories") ? [] : FINDINGS));
  });

  it("puts the pairs whose descriptions match first", async () => {
    mount();
    await screen.findByText(/both rows say ZED/);
    const table = screen.getAllByRole("table")[0];
    const whys = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getByText(/amounts match/).textContent);
    expect(whys).toEqual([
      "the amounts match and the dates are close; both rows say ZED",
      "a household member's name is on it, and the amounts match",
      "the amounts match and the dates are close",
    ]);
  });
});

const LINKED = {
  out_leg: leg("h-out", "Card", "2026-03-01", -1850),
  in_leg: leg("h-in", "Current", "2026-03-04", 1850),
  link_source: "history",
  why: "linked because these two accounts had been linked before; neither row names the other",
};

describe("Not a transfer, and links made on history alone (#131)", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) =>
        url.endsWith("/categories")
          ? []
          : {
              ...FINDINGS,
              strong: [{ ...pair("s1", "t1", "the Current row names Saver"), strength: "strong" }],
              unproven: [LINKED],
            },
      );
    vi.mocked(api.post).mockReset().mockResolvedValue({});
  });

  it("sends a pair marked Not a transfer on its own", async () => {
    mount();
    const row = (await screen.findByText("the Current row names Saver")).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Not a transfer" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(api.post).toHaveBeenCalledWith("/households/house-1/transfers/reject", {
      pairs: [{ first_id: "s1", second_id: "t1" }],
    });
  });

  it("offers Not a transfer in Worth a look too", async () => {
    mount();
    const row = (await screen.findByText(/both rows say ZED/)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Not a transfer" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0]).toEqual([
      "/households/house-1/transfers/reject",
      { pairs: [{ first_id: "o2", second_id: "i2" }] },
    ]);
  });

  it("lists links made on history alone, each with Unlink and Keep", async () => {
    mount();
    const row = (await screen.findByText(LINKED.why)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/transactions/h-out/unlink"));
    fireEvent.click(within(row).getByRole("button", { name: "Keep" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/transactions/h-out/confirm-link"));
  });
});
