// @vitest-environment jsdom

/**
 * #55's exit, screen by screen: none of the register's screens shows
 * unaccented text in the en-XA pseudo-locale. Each screen joins this file in
 * the pull request that extracts it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { activate } from "../lib/i18n";
import type { Account, Household } from "../lib/types";
import { untranslated } from "../test-pseudo";
import { Transfer } from "./Transfer";

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Casa",
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

function account(id: string, name: string, currency: string): Account {
  return {
    id,
    name,
    type: "checking",
    currency,
    closed: false,
    note: null,
    institution: null,
    country: null,
    flag: "",
    is_liability: false,
    sort_order: 0,
    statement_product: null,
    balance: 0,
    cleared: 0,
    uncleared: 0,
    transaction_count: 0,
    oldest_transaction: null,
    newest_transaction: null,
    opening_balance: 0,
    opening_date: null,
    opening_transaction_id: null,
    warnings: [],
  };
}

function withQueries(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(async () => {
  await activate("en-XA");
});

afterEach(async () => {
  cleanup();
  vi.clearAllMocks();
  await activate("en");
});

describe("in en-XA, the register's screens show no English", () => {
  it("adding a transfer, in one currency and across two", () => {
    const accounts = [account("a", "Casa", "EUR"), account("b", "Doe", "EUR"), account("c", "Sam", "SEK")];
    const { container } = render(
      withQueries(<Transfer household={HOUSEHOLD} accounts={accounts} onClose={vi.fn()} onDone={vi.fn()} />),
    );
    const [, amount] = Array.from(container.ownerDocument.querySelectorAll("input"));
    fireEvent.change(amount, { target: { value: "12.50" } });
    expect(untranslated(container.ownerDocument.body)).toEqual([]);

    const [, to] = Array.from(container.ownerDocument.querySelectorAll("select"));
    fireEvent.change(to, { target: { value: "c" } });
    const inputs = Array.from(container.ownerDocument.querySelectorAll("input"));
    fireEvent.change(inputs[2], { target: { value: "140" } });
    expect(untranslated(container.ownerDocument.body)).toEqual([]);
  });

  it("a household with one open account", () => {
    const { container } = render(
      withQueries(
        <Transfer household={HOUSEHOLD} accounts={[account("a", "Casa", "EUR")]} onClose={vi.fn()} onDone={vi.fn()} />,
      ),
    );
    expect(untranslated(container.ownerDocument.body)).toEqual([]);
  });
});
