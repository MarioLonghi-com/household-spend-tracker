// @vitest-environment jsdom

/**
 * Accounts from a file (#146), from the Accounts screen's own button.
 *
 * The server decides whether a row can be imported; what is pinned here is
 * that the panel shows that verdict as it was sent, will not offer to import
 * a file with a refused row in it, sorts its preview the way every other list
 * sorts, and commits by sending *the same file* back with dry_run off.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import { Accounts } from "./Accounts";
import type { AccountImportOut, AccountImportRow, Country, Household } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD: Household = {
  id: "house-1",
  name: "Ours",
  base_currency: "GBP",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
  colours: null,
};

const COUNTRIES: Country[] = [
  { code: "ES", name: "Spain", flag: "🇪🇸" },
  { code: "FR", name: "France", flag: "🇫🇷" },
  { code: "GB", name: "United Kingdom", flag: "🇬🇧" },
];

function row(line: number, name: string, extra: Partial<AccountImportRow> = {}): AccountImportRow {
  return {
    line,
    name,
    type: "checking",
    currency: "GBP",
    country: null,
    flag: "",
    opening_balance: null,
    opening_date: null,
    iban: null,
    problems: [],
    ...extra,
  };
}

function out(rows: AccountImportRow[], dryRun = true): AccountImportOut {
  return { dry_run: dryRun, created: dryRun ? 0 : rows.length, rows };
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Accounts household={HOUSEHOLD} />
    </QueryClientProvider>,
  );
}

/** Open the panel from the Accounts header and choose a file in it. */
async function chooseFile(): Promise<{ panel: HTMLElement; file: File }> {
  fireEvent.click(await screen.findByRole("button", { name: "Import from a file" }));
  const panel = screen.getByRole("dialog", { name: "Import accounts from a file" });
  const file = new File(["name,type\n"], "accounts.csv", { type: "text/csv" });
  const input = panel.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  return { panel, file };
}

function accountsFetches(): number {
  return vi.mocked(api.get).mock.calls.filter(([url]) => String(url).includes("/accounts")).length;
}

describe("Importing accounts from a file", () => {
  beforeEach(() => {
    vi.mocked(api.upload).mockReset();
    vi.mocked(api.get)
      .mockReset()
      .mockImplementation(
        async (url: string) => (url === "/countries" ? COUNTRIES : []) as never,
      );
  });

  it("shows the server's problems as sent and will not import while there are any", async () => {
    const said = "'bank' is not an account type -- use one of checking, savings, cash";
    vi.mocked(api.upload).mockResolvedValue(
      out([
        row(2, "Pot"),
        row(3, "Tin", { type: "bank", problems: [said, "an account needs a name"] }),
      ]),
    );
    mount();
    const { panel } = await chooseFile();

    expect(await within(panel).findByText(said)).toBeTruthy();
    expect(within(panel).getByText("an account needs a name")).toBeTruthy();
    // The file went up as a dry run first.
    const form = vi.mocked(api.upload).mock.calls[0][1] as FormData;
    expect(form.get("dry_run")).toBe("true");
    expect(
      (within(panel).getByRole("button", { name: "Import 2 accounts" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(within(panel).getByText(/1 of 2 rows have a problem/)).toBeTruthy();
  });

  it("sorts every column, money by currency first and country by name", async () => {
    vi.mocked(api.upload).mockResolvedValue(
      out([
        row(2, "eur-big", { currency: "EUR", opening_balance: 900000, country: "ES" }),
        row(3, "gbp-small", { opening_balance: 1500, country: "GB" }),
        row(4, "eur-small", { currency: "EUR", opening_balance: 500, country: "FR" }),
        row(5, "gbp-big", { opening_balance: 70000 }),
      ]),
    );
    mount();
    const { panel } = await chooseFile();
    await within(panel).findByText("gbp-big");
    const table = within(panel).getByRole("table");
    const order = () =>
      within(table)
        .getAllByRole("row")
        .slice(1)
        .map((tr) => tr.querySelector("td[data-primary]")?.textContent);

    // The file's own order until a heading is used.
    expect(order()).toEqual(["eur-big", "gbp-small", "eur-small", "gbp-big"]);

    // Base currency's group first, then EUR -- never interleaved by figure,
    // though EUR 5.00 is smaller than GBP 15.00.
    const money = within(table).getByRole("button", { name: /Opening balance/ });
    fireEvent.click(money);
    expect(order()).toEqual(["gbp-small", "gbp-big", "eur-small", "eur-big"]);
    fireEvent.click(money);
    expect(order()).toEqual(["gbp-big", "gbp-small", "eur-big", "eur-small"]);

    // France, Spain, United Kingdom -- by code it would be ES, FR, GB. The
    // row with no country goes last.
    fireEvent.click(within(table).getByRole("button", { name: /Country/ }));
    expect(order()).toEqual(["eur-small", "eur-big", "gbp-small", "gbp-big"]);

    fireEvent.click(within(table).getByRole("button", { name: /Account/ }));
    expect(order()).toEqual(["eur-big", "eur-small", "gbp-big", "gbp-small"]);

    // Figures in their own currency, from integer minor units.
    const first = within(table).getAllByRole("row")[1];
    expect(first.textContent).toContain("9,000.00");
  });

  it("sends the same file back to be kept, then closes and refreshes the accounts", async () => {
    const rows = [row(2, "Pot"), row(3, "Tin", { currency: "EUR", opening_balance: 123456 })];
    vi.mocked(api.upload)
      .mockResolvedValueOnce(out(rows))
      .mockResolvedValueOnce(out(rows, false));
    mount();
    const { panel, file } = await chooseFile();
    const confirm = await within(panel).findByRole("button", { name: "Import 2 accounts" });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    const before = accountsFetches();

    fireEvent.click(confirm);

    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));
    const [path, form] = vi.mocked(api.upload).mock.calls[1];
    expect(path).toBe("/households/house-1/accounts/import");
    expect((form as FormData).get("dry_run")).toBe("false");
    // The very file that was previewed -- no preview is kept on the server.
    expect((form as FormData).get("file")).toBe(file);

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Import accounts from a file" })).toBeNull(),
    );
    await waitFor(() => expect(accountsFetches()).toBeGreaterThan(before));
  });

  it("will not take another file, or a second confirm, while the import is being kept", async () => {
    const rows = [row(2, "Pot")];
    let finish: (value: AccountImportOut) => void = () => {};
    vi.mocked(api.upload)
      .mockResolvedValueOnce(out(rows))
      .mockImplementationOnce(
        () => new Promise<AccountImportOut>((resolve) => (finish = resolve)) as never,
      );
    mount();
    const { panel } = await chooseFile();
    const confirm = await within(panel).findByRole("button", { name: "Import 1 account" });
    fireEvent.click(confirm);
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));

    const input = panel.querySelector('input[type="file"]') as HTMLInputElement;
    await waitFor(() => expect(input.disabled).toBe(true));
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    // Even a change that reaches it anyway sends nothing.
    fireEvent.change(input, { target: { files: [new File(["x"], "other.csv")] } });
    expect(api.upload).toHaveBeenCalledTimes(2);

    finish(out(rows, false));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Import accounts from a file" })).toBeNull(),
    );
  });
});
