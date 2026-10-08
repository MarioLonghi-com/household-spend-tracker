// @vitest-environment jsdom

/**
 * #55's exit, screen by screen: none of the register's screens shows
 * unaccented text in the en-XA pseudo-locale. Each screen joins this file in
 * the pull request that extracts it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { activate } from "../lib/i18n";
import type { Account, Household } from "../lib/types";
import { untranslated } from "../test-pseudo";
import { Accounts, SuggestedIdentifiers } from "./Accounts";
import { Import } from "./Import";
import { Transfer } from "./Transfer";
import { UnprovenSection, WaitingSection } from "./TransferSections";
import { api } from "../lib/api";

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

  it("the transfers waiting for a statement, and the ones linked by history only", async () => {
    vi.mocked(api.get).mockResolvedValue([]);
    const leg = (id: string, name: string) => ({
      id,
      account_id: id,
      account_name: name,
      date: "2026-03-02",
      amount: -4250,
      currency: "EUR",
      description: "Casa",
    });
    // `why` is the server's sentence; it is data here until History and the
    // findings carry codes (#57).
    const { container } = render(
      withQueries(
        <>
          <WaitingSection household={HOUSEHOLD} waiting={[{ leg: leg("a", "Casa"), why: "Sam" }]} onChanged={vi.fn()} />
          <UnprovenSection
            household={HOUSEHOLD}
            linked={[{ out_leg: leg("a", "Casa"), in_leg: leg("b", "Doe"), link_source: "history", why: "Sam" }]}
            onChanged={vi.fn()}
          />
          <WaitingSection household={HOUSEHOLD} waiting={[]} onChanged={vi.fn()} />
          <UnprovenSection household={HOUSEHOLD} linked={[]} onChanged={vi.fn()} />
        </>,
      ),
    );
    expect(untranslated(container)).toEqual([]);
  });

  it("the accounts list, a new account and an account's settings", async () => {
    const casa = { ...account("a", "Casa", "EUR"), transaction_count: 12, oldest_transaction: "2026-01-02", newest_transaction: "2026-03-02", closed: true, is_liability: true, type: "credit_card" as const };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/countries") return [{ code: "ES", name: "Spain", flag: "" }];
      if (path.includes("/accounts")) return [casa, account("b", "Doe", "SEK")];
      if (path.endsWith("/identifiers")) return [{ id: "i1", account_id: "a", kind: "iban", value: "ES0012" }];
      return [];
    });
    render(withQueries(<Accounts household={HOUSEHOLD} />));
    await screen.findByText("Doe");
    expect(untranslated(document.body)).toEqual([]);

    // The header's primary button opens the new-account panel.
    fireEvent.click(document.querySelector("button.primary")!);
    await waitFor(() => expect(document.querySelector(".panel")).not.toBeNull());
    expect(untranslated(document.body)).toEqual([]);
  });

  it("an account's settings, with its identifiers", async () => {
    const casa = { ...account("a", "Casa", "EUR"), opening_transaction_id: "t1", opening_balance: 1000, warnings: [] };
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/countries") return [{ code: "ES", name: "Spain", flag: "" }];
      if (path.includes("/accounts")) return [casa];
      if (path.endsWith("/identifiers")) return [{ id: "i1", account_id: "a", kind: "iban", value: "ES0012" }];
      return [];
    });
    render(withQueries(<Accounts household={HOUSEHOLD} onOpenRegister={vi.fn()} />));
    await screen.findByText("Casa");
    const [, settings] = Array.from(document.querySelectorAll("td.row-actions button.link"));
    fireEvent.click(settings);
    await screen.findByText("ES0012");
    expect(untranslated(document.body)).toEqual([]);
  });

  it("the suggested identifiers", async () => {
    vi.mocked(api.get).mockResolvedValue({
      items: [
        { kind: "alias", value: "Sam", account_id: null, account_name: null, source: "pattern", why: "Sam", mentions: 3, unit: "rows", sample: "Casa", would_link: 2 },
        { kind: "file_tag", value: "X1", account_id: "a", account_name: "Casa", source: "file_name", why: "Sam", mentions: 1, unit: "files", sample: null, would_link: null },
      ],
    });
    const { container } = render(
      withQueries(<SuggestedIdentifiers household={HOUSEHOLD} accounts={[account("a", "Casa", "EUR")]} />),
    );
    await screen.findByText("Sam", { selector: ".mono" });
    expect(untranslated(container)).toEqual([]);
  });

  it("importing a statement, with one waiting to be reviewed", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/imports"))
        return [{ batch_id: "b1", filename: "casa.csv", account_id: "a", account_name: "Casa", actor_name: "Sam", staged_at: "2026-03-02T10:15:00", row_count: 3, sha256: "x" }];
      if (path.endsWith("/accounts")) return [account("a", "Casa", "EUR")];
      if (path.endsWith("/one-time-import/history")) return { imports: [] };
      return [];
    });
    render(withQueries(<Import household={HOUSEHOLD} onGo={vi.fn()} />));
    await screen.findByText("casa.csv");
    // whenStaged writes the month as a word, the browser's way; it is not ours.
    const left = untranslated(document.body).filter((word) => !/^(March|AM|PM|at)$/.test(word));
    expect(left).toEqual([]);
  });
});
