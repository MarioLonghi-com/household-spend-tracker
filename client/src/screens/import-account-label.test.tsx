// @vitest-environment jsdom

/**
 * How the Import screen's "Into which account" dropdown names an account
 * (#365): name, then bank, then currency. Asserted on the options a person
 * reads, with two accounts at two banks in two currencies and a third with
 * no bank at all, so a label that took the first account's bank for every
 * row, or printed a bar before nothing, would show.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Import, importAccountLabel } from "./Import";
import type { Account, Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
} as unknown as Household;

const ACCOUNTS = [
  { id: "acct-current", name: "Current", currency: "EUR", institution: "Harbour Bank" },
  { id: "acct-pounds", name: "Savings", currency: "GBP", institution: "Northern Mutual" },
  { id: "acct-cash", name: "Cash tin", currency: "EUR", institution: null },
] as unknown as Account[];

describe("an account in the Import screen's dropdown", () => {
  it("reads as name, then bank, then currency, and without a bar when there is no bank", () => {
    expect(importAccountLabel(ACCOUNTS[0])).toBe("Current | Harbour Bank (EUR)");
    expect(importAccountLabel(ACCOUNTS[1])).toBe("Savings | Northern Mutual (GBP)");
    expect(importAccountLabel(ACCOUNTS[2])).toBe("Cash tin (EUR)");
    // A bank of only spaces is no bank.
    expect(importAccountLabel({ name: "Wallet", currency: "GBP", institution: "  " })).toBe(
      "Wallet (GBP)",
    );
  });

  describe("on the screen", () => {
    beforeEach(() => {
      vi.mocked(api.get)
        .mockReset()
        .mockImplementation(async (url: string) => (url.endsWith("/accounts") ? ACCOUNTS : []));
    });

    it("lists every account that way, each with its own bank", async () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <Import household={HOUSEHOLD} />
        </QueryClientProvider>,
      );
      await screen.findByRole("option", { name: "Current | Harbour Bank (EUR)" });
      const picker = screen.getByRole("combobox", { name: /Into which account/ });
      expect(within(picker).getAllByRole("option").map((one) => one.textContent)).toEqual([
        "Choose…",
        "Current | Harbour Bank (EUR)",
        "Savings | Northern Mutual (GBP)",
        "Cash tin (EUR)",
      ]);
    });
  });
});
