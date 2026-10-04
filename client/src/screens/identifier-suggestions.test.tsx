// @vitest-environment jsdom

/**
 * Identifiers the ledger suggests (#130), where a person sees them: the
 * Accounts screen's list, the Transfers screen's one line, and the Import
 * screen's offer of a file's tag. Asserted on what each sends -- nothing is
 * ever added without a click, and a suggestion that matched no account is
 * added to the one the person picked.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { SuggestedIdentifiers } from "./Accounts";
import { Import } from "./Import";
import { Transfers } from "./Transfers";
import type { Account, Household, IdentifierSuggestions } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = {
  id: "house-1",
  name: "Ours",
  base_currency: "EUR",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  colours: null,
} as unknown as Household;

const ACCOUNTS = [
  { id: "acct-current", name: "Current", currency: "EUR" },
  { id: "acct-pocket", name: "Rainy Day Fund", currency: "EUR" },
] as unknown as Account[];

const SUGGESTIONS: IdentifierSuggestions = {
  items: [
    {
      kind: "alias",
      value: "Umbrella Money",
      account_id: "acct-pocket",
      account_name: "Rainy Day Fund",
      source: "pattern",
      why: "Rainy Day Fund's own statement quotes it",
      mentions: 12,
      unit: "rows",
      sample: "Deposit to 'Umbrella Money'",
      would_link: 5,
    },
    {
      kind: "number",
      value: "11112222",
      account_id: null,
      account_name: null,
      source: "pattern",
      why: "an A/C number in the descriptions, and it is not one of your accounts",
      mentions: 6,
      unit: "rows",
      sample: "TO A/C 11112222",
      would_link: null,
    },
    {
      kind: "card",
      value: "4242",
      account_id: "acct-current",
      account_name: "Current",
      source: "pattern",
      why: "a card's last four in the descriptions",
      mentions: 3,
      unit: "rows",
      sample: null,
      would_link: 1,
    },
  ],
  would_link: 6,
};

function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("Suggested identifiers on the Accounts screen", () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset().mockResolvedValue(SUGGESTIONS);
    vi.mocked(api.post).mockReset().mockResolvedValue({});
  });

  it("adds a suggestion to the account it was suggested for", async () => {
    mount(<SuggestedIdentifiers household={HOUSEHOLD} accounts={ACCOUNTS} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Umbrella Money" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0]).toEqual([
      "/households/house-1/identifiers/suggestions/add",
      { kind: "alias", value: "Umbrella Money", account_id: "acct-pocket" },
    ]);
  });

  it("waits for an account when the suggestion matched none", async () => {
    mount(<SuggestedIdentifiers household={HOUSEHOLD} accounts={ACCOUNTS} />);
    const add = await screen.findByRole("button", { name: "Add 11112222" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("combobox", { name: "Account for 11112222" }), {
      target: { value: "acct-current" },
    });
    fireEvent.click(add);
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0][1]).toEqual({
      kind: "number",
      value: "11112222",
      account_id: "acct-current",
    });
  });

  it("ignores one by its kind and value", async () => {
    mount(<SuggestedIdentifiers household={HOUSEHOLD} accounts={ACCOUNTS} />);
    fireEvent.click(await screen.findByRole("button", { name: "Ignore 4242" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0]).toEqual([
      "/households/house-1/identifiers/suggestions/ignore",
      { kind: "card", value: "4242" },
    ]);
  });

  it("shows the list before its pair counts, then fills them in (#232)", async () => {
    let release: (value: IdentifierSuggestions) => void = () => {};
    const counts = new Promise<IdentifierSuggestions>((resolve) => (release = resolve));
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) =>
        url.endsWith("?count_pairs=1")
          ? counts
          : {
              items: SUGGESTIONS.items.map((one) => ({ ...one, would_link: null })),
              would_link: null,
            },
      );
    mount(<SuggestedIdentifiers household={HOUSEHOLD} accounts={ACCOUNTS} />);
    const row = (await screen.findByText("Umbrella Money")).closest("tr")!;
    expect(within(row).getByTitle("not counted")).toBeTruthy();
    expect(vi.mocked(api.get).mock.calls.map(([url]) => url).sort()).toEqual([
      "/households/house-1/identifiers/suggestions",
      "/households/house-1/identifiers/suggestions?count_pairs=1",
    ]);

    release(SUGGESTIONS);
    await waitFor(() =>
      expect(
        within(screen.getByText("Umbrella Money").closest("tr")!).getByText("5 pairs"),
      ).toBeTruthy(),
    );
  });

  it("sorts at its headings: most pairs first, then by value", async () => {
    mount(<SuggestedIdentifiers household={HOUSEHOLD} accounts={ACCOUNTS} />);
    await screen.findByText("Umbrella Money");
    const table = screen.getByRole("table");
    const order = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((row) => row.querySelector(".mono")!.textContent);
    // Not counted sorts last, whichever way round.
    expect(order()).toEqual(["Umbrella Money", "4242", "11112222"]);
    fireEvent.click(within(table).getByRole("button", { name: /Value/ }));
    expect(order()).toEqual(["11112222", "4242", "Umbrella Money"]);
    fireEvent.click(within(table).getByRole("button", { name: /Mentions/ }));
    expect(order()).toEqual(["4242", "11112222", "Umbrella Money"]);
    fireEvent.click(within(table).getByRole("button", { name: /Mentions/ }));
    expect(order()).toEqual(["Umbrella Money", "11112222", "4242"]);
  });
});

describe("The Transfers screen's summary", () => {
  it("says how many suggestions would link how many pairs", async () => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) =>
        url.endsWith("/identifiers/suggestions?count_pairs=1")
          ? SUGGESTIONS
          : { strong: [], suggested: [], awaiting: [], unproven: [] },
      );
    mount(<Transfers household={HOUSEHOLD} />);
    const line = await screen.findByTestId("suggested-identifiers");
    expect(line.textContent).toMatch(/^2 suggested identifiers would link 6 pairs\./);
    // Only the counted list: the uncounted one has nothing to say here.
    const asked = vi.mocked(api.get).mock.calls.map(([url]) => url);
    expect(asked.filter((url) => url.includes("/identifiers/suggestions"))).toEqual([
      "/households/house-1/identifiers/suggestions?count_pairs=1",
    ]);
  });
});

describe("The Import screen's tag offer", () => {
  beforeEach(() => {
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(async (url: string) => (url.endsWith("/accounts") ? ACCOUNTS : []));
    vi.mocked(api.post).mockReset().mockResolvedValue({});
    vi.mocked(api.upload)
      .mockReset()
      .mockResolvedValue({
        account_id: null,
        account_name: null,
        how: null,
        tag_kind: "file_tag",
        tag: "d4e5f6",
      });
  });

  it("offers the file name's tag for the account picked, and adds it only when asked", async () => {
    mount(<Import household={HOUSEHOLD} />);
    await screen.findByRole("option", { name: "Rainy Day Fund (EUR)" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["Date,Description,Amount\n"], "statement_en-gb_d4e5f6.csv");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(api.upload).toHaveBeenCalled());
    // Nothing is offered until there is an account to offer it for.
    expect(screen.queryByTestId("tag-offer")).toBeNull();

    fireEvent.change(screen.getByRole("combobox", { name: /Into which account/ }), {
      target: { value: "acct-pocket" },
    });
    const offer = await screen.findByTestId("tag-offer");
    expect(offer.textContent).toContain("d4e5f6");
    expect(offer.textContent).toContain("Rainy Day Fund's file tag");
    expect(api.post).not.toHaveBeenCalled();

    fireEvent.click(within(offer).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0]).toEqual([
      "/households/house-1/identifiers/suggestions/add",
      { kind: "file_tag", value: "d4e5f6", account_id: "acct-pocket" },
    ]);
    await waitFor(() => expect(screen.queryByTestId("tag-offer")).toBeNull());
  });
});
