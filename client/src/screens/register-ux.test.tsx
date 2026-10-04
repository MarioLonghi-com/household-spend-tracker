// @vitest-environment jsdom

/**
 * The register's layout and handling changes of #143, as behaviour.
 *
 * Two accounts in two currencies, two ordinary rows in each, both legs of one
 * transfer, and one work expense -- so a check that only looked at the first
 * row, or only at rows of one kind, cannot pass by accident. Twelve
 * categories under two groups: more than the eight a combobox used to stop
 * at, so "lists every category" is a claim this fixture can refute.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
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

import { api } from "../lib/api";
import { Register } from "./Register";
import type { RegisterPreset } from "./Register";
import type { Household, Transaction } from "../lib/types";

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function row(
  over: Partial<Transaction> & { id: string; currency?: string },
): Transaction & { currency: string } {
  return {
    account_id: "acc-eur",
    date: "2026-03-24",
    amount: -1200,
    payee_id: null,
    payee_name: "Shop",
    category_id: null,
    category_name: null,
    memo: null,
    cleared: "uncleared",
    transfer_account_id: null,
    transfer_transaction_id: null,
    import_id: null,
    split_id: null,
    running_balance: null,
    has_receipt: false,
    reimbursement: null,
    reimbursed_by_id: null,
    currency: "EUR",
    ...over,
  };
}

const ROWS = [
  row({
    id: "bakery",
    payee_name: "Bakery",
    category_id: "cat-0",
    category_name: "Everyday: Groceries",
  }),
  row({ id: "cinema", payee_name: "Cinema", amount: -900, memo: "Friday night" }),
  row({ id: "newsagent", account_id: "acc-gbp", currency: "GBP", payee_name: "Newsagent" }),
  row({
    id: "taxi",
    account_id: "acc-gbp",
    currency: "GBP",
    payee_name: "Taxi",
    memo: "Airport run for the client visit",
    reimbursement: "expected",
  }),
  row({
    id: "leg-out",
    payee_name: "Transfer : Savings",
    amount: -5000,
    transfer_account_id: "acc-gbp",
    transfer_transaction_id: "leg-in",
  }),
  row({
    id: "leg-in",
    account_id: "acc-gbp",
    currency: "GBP",
    payee_name: "Transfer : Current",
    amount: 4300,
    transfer_account_id: "acc-eur",
    transfer_transaction_id: "leg-out",
  }),
];

const CATEGORY_NAMES = [
  "Groceries", "Eating out", "Health", "Transport", "Clothes", "Gifts",
  "Rent", "Electricity", "Water", "Internet", "Phone", "Insurance",
];
const CATEGORIES = [
  {
    id: "grp-everyday",
    name: "Everyday",
    categories: CATEGORY_NAMES.slice(0, 6).map((name, at) => ({
      id: `cat-${at}`,
      name,
      full_name: `Everyday: ${name}`,
    })),
  },
  {
    id: "grp-bills",
    name: "Bills",
    categories: CATEGORY_NAMES.slice(6).map((name, at) => ({
      id: `cat-${at + 6}`,
      name,
      full_name: `Bills: ${name}`,
    })),
  },
];

let asked: URLSearchParams[] = [];

beforeEach(() => {
  window.localStorage.clear();
  asked = [];
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    // The badge's count beside Needs a category (#188) asks for one row;
    // it is not the register's request.
    if (path.includes("/transactions?") && !path.includes("limit=1")) {
      asked.push(new URLSearchParams(path.split("?")[1]));
      return Promise.resolve({
        transactions: ROWS,
        total: ROWS.length,
        has_running_balance: false,
        capped: false,
      });
    }
    if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP"] });
    if (path.endsWith("/categories")) return Promise.resolve(CATEGORIES);
    if (path.endsWith("/accounts"))
      return Promise.resolve([
        { id: "acc-eur", name: "Current", currency: "EUR", type: "checking", country: "ES", flag: "🇪🇸" },
        { id: "acc-gbp", name: "Savings", currency: "GBP", type: "savings", country: "GB", flag: "🇬🇧" },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

async function mount(preset?: RegisterPreset) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} preset={preset} />
    </QueryClientProvider>,
  );
  await screen.findByText("Bakery");
  return view;
}

const last = () => asked[asked.length - 1];
const stored = (name: string) =>
  JSON.parse(window.localStorage.getItem(`spendtracker.register.house-1.${name}`) ?? "null");

function rowOf(payee: string): HTMLElement {
  return screen.getByText(payee).closest("tr") as HTMLElement;
}

// --------------------------------------------------------------------------- //
// The category drop-down
// --------------------------------------------------------------------------- //

describe("the category cell's drop-down", () => {
  it("lists every category when opened on an uncategorised row", async () => {
    await mount();
    fireEvent.click(within(rowOf("Cinema")).getByRole("button", { name: "uncategorised" }));
    const listed = within(screen.getByRole("listbox")).getAllByRole("option");
    // All twelve, where it used to stop at eight.
    expect(listed.map((one) => one.textContent)).toEqual(
      CATEGORIES.flatMap((group) => group.categories.map((one) => one.full_name)),
    );
  });

  it("lists every category when opened on a categorised row, its own included", async () => {
    await mount();
    fireEvent.click(within(rowOf("Bakery")).getByRole("button", { name: /Everyday: Groceries/ }));
    const box = screen.getByRole("combobox", { name: "Category" }) as HTMLInputElement;
    expect(box.value).toBe("Everyday: Groceries");
    // It was ranked against the row's own category: the names containing
    // "Everyday: Groceries", minus that one, which is to say none.
    const listed = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(listed).toHaveLength(12);
    expect(listed.map((one) => one.textContent)).toContain("Everyday: Groceries");
    expect(listed.map((one) => one.textContent)).toContain("Bills: Insurance");
  });

  it("still narrows as soon as something is typed", async () => {
    await mount();
    fireEvent.click(within(rowOf("Cinema")).getByRole("button", { name: "uncategorised" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Category" }), {
      target: { value: "in" },
    });
    const listed = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(listed.map((one) => one.textContent)).toEqual([
      "Everyday: Eating out",
      "Bills: Internet",
      "Bills: Insurance",
    ]);
  });
});

// --------------------------------------------------------------------------- //
// Remembered settings
// --------------------------------------------------------------------------- //

describe("the register's remembered settings", () => {
  it("brings every filter, the sort and the grouping back after the screen is left", async () => {
    const first = await mount();
    fireEvent.change(screen.getByPlaceholderText("payee, memo, or the bank's own words"), {
      target: { value: "taxi" },
    });
    fireEvent.change(screen.getByPlaceholderText("in or out, any currency"), {
      target: { value: "12" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Cleared" }), {
      target: { value: "cleared" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Source" }), {
      target: { value: "imported" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Work expenses" }), {
      target: { value: "owed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Last year" }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "Currencies shown" })).getByRole("button", { name: "GBP" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Type" }));
    fireEvent.click(screen.getByRole("button", { name: "Payee" }));
    // Select none leaves the backlog as the only tick: the old checkbox.
    fireEvent.click(screen.getByRole("button", { name: /All categories/ }));
    fireEvent.click(
      within(screen.getByRole("group", { name: "All categories" })).getByRole("button", {
        name: "Select none",
      }),
    );
    await waitFor(() => expect(last().get("search")).toBe("taxi"), { timeout: 2000 });
    await waitFor(() => expect(last().get("amount")).toBe("12"), { timeout: 2000 });
    const before = last().toString();
    first.unmount();

    // Somewhere else, and back.
    asked = [];
    await mount();
    await waitFor(() => expect(last().get("search")).toBe("taxi"), { timeout: 2000 });
    expect(last().toString()).toBe(before);
    expect(last().get("cleared")).toBe("cleared");
    expect(last().get("source")).toBe("imported");
    expect(last().get("reimbursement")).toBe("owed");
    expect(last().get("uncategorised")).toBe("true");
    expect(last().get("since")).toMatch(/^\d{4}-01-01$/);
    // Needs a category turns the order round; the payee sort came first and
    // the date sort it set is what was left.
    expect(last().get("sort")).toBe("date");
    expect(last().get("direction")).toBe("asc");
    // And the boxes say so, not only the request.
    expect(
      (screen.getByPlaceholderText("in or out, any currency") as HTMLInputElement).value,
    ).toBe("12");
    expect(
      within(screen.getByRole("group", { name: "Currencies shown" }))
        .getByRole("button", { name: "GBP" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(stored("currencies")).toEqual(["EUR"]);
    expect(screen.getByRole("button", { name: "Type" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Country" }).getAttribute("aria-pressed")).toBe("false");
  });

  it("does not remember the selection", async () => {
    const first = await mount();
    fireEvent.click(within(rowOf("Cinema")).getByRole("checkbox"));
    expect(screen.getByText(/1 selected\./)).toBeTruthy();
    first.unmount();
    await mount();
    expect(screen.queryByText(/selected\./)).toBeNull();
    expect((within(rowOf("Cinema")).getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  });

  it("lets a preset win without writing over what was remembered", async () => {
    const first = await mount();
    fireEvent.change(screen.getByPlaceholderText("payee, memo, or the bank's own words"), {
      target: { value: "bakery" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Cleared" }), {
      target: { value: "uncleared" },
    });
    await waitFor(() => expect(last().get("search")).toBe("bakery"), { timeout: 2000 });
    first.unmount();

    // The report's "Show in Transactions": its filter, and nothing of ours
    // that could hide the rows it promised.
    asked = [];
    const sentHere = await mount({ reimbursement: "owed" });
    await waitFor(() => expect(last().get("reimbursement")).toBe("owed"));
    expect(last().get("search")).toBeNull();
    expect(last().get("cleared")).toBeNull();
    // Shown, not kept.
    expect(stored("work")).toBeNull();
    expect(stored("search")).toBe("bakery");
    sentHere.unmount();

    // From the menu again: what the person set, not what the report did.
    asked = [];
    await mount();
    await waitFor(() => expect(last().get("search")).toBe("bakery"), { timeout: 2000 });
    expect(last().get("cleared")).toBe("uncleared");
    expect(last().get("reimbursement")).toBeNull();
  });

  it("keeps a change made under a preset", async () => {
    const sentHere = await mount({ reimbursement: "owed" });
    fireEvent.change(screen.getByRole("combobox", { name: "Work expenses" }), {
      target: { value: "paid" },
    });
    await waitFor(() => expect(last().get("reimbursement")).toBe("paid"));
    expect(stored("work")).toBe("paid");
    sentHere.unmount();
    asked = [];
    await mount();
    await waitFor(() => expect(last().get("reimbursement")).toBe("paid"));
  });

  it("reads a damaged or foreign value as the default", async () => {
    window.localStorage.setItem("spendtracker.register.house-1.work", JSON.stringify("everything"));
    window.localStorage.setItem("spendtracker.register.house-1.sort", JSON.stringify("colour"));
    window.localStorage.setItem(
      "spendtracker.register.house-1.range",
      JSON.stringify({ since: "last tuesday", until: null }),
    );
    window.localStorage.setItem("spendtracker.register.house-1.cleared", "{not json");
    await mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    expect(last().get("reimbursement")).toBeNull();
    expect(last().get("sort")).toBe("date");
    expect(last().get("since")).toBeNull();
    expect(last().get("cleared")).toBeNull();
  });

  it("drops a remembered account that no longer exists", async () => {
    window.localStorage.setItem(
      "spendtracker.register.house-1.accounts",
      JSON.stringify(["acc-gbp", "acc-closed-long-ago"]),
    );
    await mount();
    await waitFor(() => expect(stored("accounts")).toEqual(["acc-gbp"]));
    await waitFor(() => expect(last().getAll("account_id")).toEqual(["acc-gbp"]));
  });

  it("goes back to every account when none of the remembered ones exist", async () => {
    window.localStorage.setItem(
      "spendtracker.register.house-1.accounts",
      JSON.stringify(["gone-1", "gone-2"]),
    );
    await mount();
    await waitFor(() => expect(stored("accounts")).toBeNull());
    await waitFor(() => expect(last().getAll("account_id")).toEqual([]));
  });
});

// --------------------------------------------------------------------------- //
// Select all
// --------------------------------------------------------------------------- //

describe("the heading's select-all box", () => {
  const all = () => screen.getByRole("checkbox", { name: "Select every row shown" }) as HTMLInputElement;
  const ticked = () =>
    ROWS.filter((one) => (within(rowOf(one.payee_name!)).getByRole("checkbox") as HTMLInputElement).checked)
      .map((one) => one.id);

  it("ticks every row the filter shows, and a second click unticks them", async () => {
    await mount();
    expect(all().checked).toBe(false);
    expect(all().indeterminate).toBe(false);
    fireEvent.click(all());
    expect(ticked()).toEqual(ROWS.map((one) => one.id));
    expect(screen.getByText(/6 selected\./)).toBeTruthy();
    expect(all().checked).toBe(true);
    fireEvent.click(all());
    expect(ticked()).toEqual([]);
    expect(screen.queryByText(/selected\./)).toBeNull();
  });

  it("goes indeterminate when only some are ticked, and fills them in from there", async () => {
    await mount();
    fireEvent.click(within(rowOf("Cinema")).getByRole("checkbox"));
    fireEvent.click(within(rowOf("Taxi")).getByRole("checkbox"));
    expect(all().checked).toBe(false);
    expect(all().indeterminate).toBe(true);
    fireEvent.click(all());
    expect(ticked()).toHaveLength(ROWS.length);
    expect(all().indeterminate).toBe(false);
    // Unticking one row takes the box back to the third state.
    fireEvent.click(within(rowOf("Bakery")).getByRole("checkbox"));
    expect(all().indeterminate).toBe(true);
    expect(all().checked).toBe(false);
  });

  it("means the rows the currency toggle leaves, not the ones it hides", async () => {
    await mount();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Currencies shown" })).getByRole("button", { name: "GBP" }),
    );
    fireEvent.click(all());
    // The three euro rows; the pound rows are not on screen to tick.
    expect(screen.getByText(/3 selected\./)).toBeTruthy();
    expect(all().checked).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// The heading line
// --------------------------------------------------------------------------- //

describe("the heading line", () => {
  it("is called Transactions, and has the currency toggle straight after it", async () => {
    await mount();
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading.textContent).toBe("Transactions");
    const next = heading.nextElementSibling as HTMLElement;
    expect(next.className).toBe("currency-toggle");
    expect(within(next).getByRole("group", { name: "Currencies shown" })).toBeTruthy();
    // And no longer in the filter line.
    expect(document.querySelector(".filter-line .currency-toggle")).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// The Add menu
// --------------------------------------------------------------------------- //

describe("the Add transaction menu", () => {
  const add = () => screen.getByRole("button", { name: "Add transaction" });

  it("replaces the separate Add transfer button", async () => {
    await mount();
    expect(screen.queryByRole("button", { name: /Add transfer/ })).toBeNull();
    expect(add().getAttribute("aria-haspopup")).toBe("menu");
    expect(add().getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("opens the add panel from Single transaction", async () => {
    await mount();
    fireEvent.click(add());
    const menu = screen.getByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((one) => one.textContent)).toEqual([
      "Single transaction",
      "Transfer",
    ]);
    // Focus goes to the first choice, so the keyboard can carry on.
    expect(document.activeElement?.textContent).toBe("Single transaction");
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Single transaction" }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("heading", { name: "Add a transaction" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Add transfer between accounts" })).toBeNull();
  });

  it("opens the transfer panel from Transfer", async () => {
    await mount();
    fireEvent.click(add());
    fireEvent.click(screen.getByRole("menuitem", { name: "Transfer" }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("heading", { name: "Add transfer between accounts" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Add a transaction" })).toBeNull();
  });

  it("is worked by the keyboard, and Escape shuts it and hands focus back", async () => {
    await mount();
    add().focus();
    fireEvent.keyDown(add(), { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("Single transaction");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("Transfer");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("Single transaction");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(document.activeElement?.textContent).toBe("Transfer");
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(add());
    // Nothing was opened by the cancel.
    expect(screen.queryByRole("heading", { name: "Add a transaction" })).toBeNull();
  });

  it("shuts on a click anywhere else", async () => {
    await mount();
    fireEvent.click(add());
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole("heading", { level: 1 }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(add().getAttribute("aria-expanded")).toBe("false");
  });
});

// --------------------------------------------------------------------------- //
// The transfer mark
// --------------------------------------------------------------------------- //

describe("the transfer column", () => {
  const cellOf = (payee: string) => rowOf(payee).querySelector("td.transfer-col") as HTMLElement;

  it("sits between the tick box and the date, and marks both legs and nothing else", async () => {
    await mount();
    const cells = [...rowOf("Transfer : Savings").children] as HTMLElement[];
    expect(cells[0].dataset.select).toBe("true");
    expect(cells[1].className).toBe("transfer-col");
    expect(cells[2].dataset.label).toBe("Date");
    // Its own column, not the Source one: the T is still there too.
    expect(cellOf("Transfer : Savings").textContent).toBe("⇄");
    expect(rowOf("Transfer : Savings").querySelector('td[data-label="Source"]')?.textContent).toContain("T");
    expect(
      within(cellOf("Transfer : Current")).getByLabelText("Transfer — the other side is in Current"),
    ).toBeTruthy();
    for (const payee of ["Bakery", "Cinema", "Newsagent", "Taxi"]) {
      expect(cellOf(payee).textContent).toBe("");
    }
  });

  it("is there when no transfer is in view, so nothing shifts", async () => {
    const noTransfers = ROWS.filter((one) => !one.transfer_account_id);
    const answer = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path.includes("/transactions?")
        ? Promise.resolve({
            transactions: noTransfers,
            total: noTransfers.length,
            has_running_balance: false,
            capped: false,
          })
        : answer(path)) as typeof api.get);
    await mount();
    expect(screen.queryByText("Transfer : Savings")).toBeNull();
    expect(document.querySelector("thead th.transfer-col")?.textContent).toBe("Transfer");
    const cells = document.querySelectorAll("tbody td.transfer-col");
    expect(cells).toHaveLength(noTransfers.length);
    expect([...cells].every((one) => one.textContent === "")).toBe(true);
    // Every row still has as many cells as there are headings.
    const headings = document.querySelectorAll("thead th").length;
    for (const tr of document.querySelectorAll("tbody tr")) expect(tr.children).toHaveLength(headings);
  });
});

// --------------------------------------------------------------------------- //
// The W in the memo
// --------------------------------------------------------------------------- //

describe("the work-expense W", () => {
  const memoOf = (payee: string) => rowOf(payee).querySelector('td[data-label="Memo"]') as HTMLElement;
  const sourceOf = (payee: string) => rowOf(payee).querySelector('td[data-label="Source"]') as HTMLElement;

  it("sits at the right-hand end of the memo cell, after the text, and not by the source", async () => {
    await mount();
    const line = memoOf("Taxi").querySelector(".memo-line") as HTMLElement;
    const parts = [...line.children] as HTMLElement[];
    expect(parts).toHaveLength(2);
    expect(parts[0].textContent).toBe("Airport run for the client visit");
    expect(parts[1].className).toBe("tag work-owed");
    expect(parts[1].getAttribute("aria-label")).toBe("Work expense — not reimbursed yet");
    expect(sourceOf("Taxi").querySelector(".tag.work-owed")).toBeNull();
    // The other rows have a memo line with no pill in it.
    expect(memoOf("Cinema").querySelector(".tag")).toBeNull();
    expect(memoOf("Cinema").textContent).toBe("Friday night");
  });

  it("still does what a click on it did: selects the row, and does not open the memo", async () => {
    await mount();
    fireEvent.click(memoOf("Taxi").querySelector(".tag.work-owed")!);
    expect((within(rowOf("Taxi")).getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Memo" })).toBeNull();
    // The text beside it is still the editor.
    fireEvent.click(within(memoOf("Taxi")).getByRole("button", { name: /edit the memo/ }));
    expect((screen.getByRole("textbox", { name: "Memo" }) as HTMLInputElement).value).toBe(
      "Airport run for the client visit",
    );
  });

  it("keeps a flagged row with no memo from counting as an empty memo", async () => {
    const flagged = ROWS.map((one) => (one.id === "taxi" ? { ...one, memo: null } : one));
    const answer = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path.includes("/transactions?")
        ? Promise.resolve({ transactions: flagged, total: flagged.length, has_running_balance: false, capped: false })
        : answer(path)) as typeof api.get);
    await mount();
    // On a phone an empty memo's line is dropped -- and would take the W with it.
    expect(memoOf("Taxi").dataset.empty).toBeUndefined();
    expect(memoOf("Cinema").dataset.empty).toBeUndefined();
    expect(memoOf("Bakery").dataset.empty).toBe("true");
  });
});

// --------------------------------------------------------------------------- //
// The table's text size
// --------------------------------------------------------------------------- //

describe("the table's text size", () => {
  const smaller = () => screen.getByRole("button", { name: "Smaller text in the table" }) as HTMLButtonElement;
  const larger = () => screen.getByRole("button", { name: "Larger text in the table" }) as HTMLButtonElement;
  const table = () => document.querySelector(".register-card table") as HTMLElement;
  const size = () => table().style.getPropertyValue("--register-text");
  const pad = () => table().style.getPropertyValue("--register-pad");

  it("sits immediately left of Add transaction", async () => {
    await mount();
    const group = screen.getByRole("group", { name: "Table text size" });
    expect(group.nextElementSibling?.querySelector("button")?.textContent).toContain("Add transaction");
  });

  it("steps down to a floor and up to a ceiling, with the padding following", async () => {
    await mount();
    expect(size()).toBe("14px");
    expect(pad()).toBe("7px");
    for (const expected of ["13px", "12px", "11px"]) {
      fireEvent.click(smaller());
      expect(size()).toBe(expected);
    }
    expect(pad()).toBe("2px");
    // The floor: the button says so, and a click does nothing.
    expect(smaller().disabled).toBe(true);
    fireEvent.click(smaller());
    expect(size()).toBe("11px");
    for (const expected of ["12px", "13px", "14px", "16px"]) {
      fireEvent.click(larger());
      expect(size()).toBe(expected);
    }
    expect(pad()).toBe("9px");
    expect(larger().disabled).toBe(true);
    expect(smaller().disabled).toBe(false);
    fireEvent.click(larger());
    expect(size()).toBe("16px");
  });

  it("is remembered with the filters, and a stored size out of range is ignored", async () => {
    const first = await mount();
    fireEvent.click(smaller());
    fireEvent.click(smaller());
    expect(size()).toBe("12px");
    first.unmount();
    await mount();
    expect(size()).toBe("12px");
    cleanup();

    window.localStorage.setItem("spendtracker.register.house-1.text", JSON.stringify(9));
    await mount();
    expect(size()).toBe("14px");
  });
});

// --------------------------------------------------------------------------- //
// The accounts picker's label
// --------------------------------------------------------------------------- //

describe("the accounts picker", () => {
  it("is labelled like the boxes beside it, and the label's two words switch the grouping", async () => {
    await mount();
    const field = document.querySelector(".filter-line .picker-with-mode") as HTMLElement;
    expect(field.classList.contains("field")).toBe(true);
    expect(field.querySelector(".field-head")?.textContent?.replace(/\s+/g, "")).toBe(
      "Accounts(Country/Type)",
    );
    // The separate select is gone.
    expect(screen.queryByRole("combobox", { name: "Group accounts by" })).toBeNull();
    const group = within(field).getByRole("group", { name: "Group accounts by" });
    const country = within(group).getByRole("button", { name: "Country" });
    const type = within(group).getByRole("button", { name: "Type" });
    expect(country.getAttribute("aria-pressed")).toBe("true");
    expect(type.getAttribute("aria-pressed")).toBe("false");

    // Open the picker: gathered by country, flag and name.
    fireEvent.click(within(field).getByRole("button", { name: /All accounts/ }));
    const headings = () =>
      [...document.querySelectorAll(".picker-head span")].map((one) => one.textContent);
    expect(headings()).toEqual(["🇪🇸 ES", "🇬🇧 GB"]);

    fireEvent.click(type);
    expect(type.getAttribute("aria-pressed")).toBe("true");
    expect(country.getAttribute("aria-pressed")).toBe("false");
    expect(headings()).toEqual(["Checking", "Savings"]);
  });
});

// --------------------------------------------------------------------------- //
// The selection banner's place
// --------------------------------------------------------------------------- //

describe("the selection banner", () => {
  it("is docked after the table, at the end of the card, with the count line left above", async () => {
    await mount();
    const count = document.querySelector(".register-count") as HTMLElement;
    const scroller = document.querySelector(".table-scroll") as HTMLElement;
    const card = document.querySelector(".register-card") as HTMLElement;
    expect(count.textContent).toBe("6 transactions");
    // No selection, no dock.
    expect(document.querySelector(".selection-dock")).toBeNull();

    fireEvent.click(within(rowOf("Cinema")).getByRole("checkbox"));
    fireEvent.click(within(rowOf("Taxi")).getByRole("checkbox"));
    const banner = screen.getByText(/2 selected\./).closest(".banner") as HTMLElement;
    const dock = banner.parentElement as HTMLElement;
    expect(dock.className).toBe("selection-dock");
    // The count line stays where #143 put it, above the rows, and still counts
    // the selection -- but nothing of the banner is next to it any more.
    expect(count.textContent).toBe("6 transactions · 2 selected");
    expect(count.nextElementSibling).toBeNull();
    expect(count.compareDocumentPosition(scroller) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // After the rows: the dock follows the scroller, and is the card's last
    // child, which is what the stylesheet pins to the bottom.
    expect(scroller.compareDocumentPosition(dock) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.lastElementChild).toBe(dock);
    // The sums came along: two currencies, a line each.
    expect([...banner.querySelectorAll(".sum-line .mono")].map((one) => one.textContent)).toEqual([
      "EUR",
      "GBP",
    ]);
    // And there is no banner left before the table.
    let before = scroller.previousElementSibling;
    while (before) {
      expect(before.querySelector(".banner.info")).toBeNull();
      before = before.previousElementSibling;
    }
  });

  it("keeps a bulk act's note in the dock after the selection it came from is gone", async () => {
    vi.mocked(api.post).mockResolvedValueOnce({
      transactions: [{ id: "cinema" }, { id: "leg-out" }],
      skipped_transfer_legs: 1,
    });
    await mount();
    fireEvent.click(within(rowOf("Cinema")).getByRole("checkbox"));
    fireEvent.click(within(rowOf("Transfer : Savings")).getByRole("checkbox"));
    fireEvent.change(screen.getByRole("combobox", { name: "Set the category on the selected rows" }), {
      target: { value: "cat-1" },
    });
    const note = await screen.findByRole("status");
    expect(note.textContent).toMatch(/^Set the category on 1 row and skipped 1 transfer leg — /);
    // The act cleared the selection, so the banner is gone and the count line
    // no longer counts one -- and the note is where the banner was.
    expect(screen.queryByText(/selected\./)).toBeNull();
    expect((document.querySelector(".register-count") as HTMLElement).textContent).toBe(
      "6 transactions",
    );
    const dock = note.parentElement as HTMLElement;
    expect(dock.className).toBe("selection-dock");
    expect((document.querySelector(".register-card") as HTMLElement).lastElementChild).toBe(dock);
    fireEvent.click(within(note).getByRole("button", { name: "OK" }));
    expect(document.querySelector(".selection-dock")).toBeNull();
  });

  it("says a refused bulk act in the dock, not above the table", async () => {
    vi.mocked(api.post).mockRejectedValueOnce(new Error("Those rows changed under you."));
    await mount();
    fireEvent.click(within(rowOf("Cinema")).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Mark cleared" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Those rows changed under you.");
    const dock = alert.parentElement as HTMLElement;
    expect(dock.className).toBe("selection-dock");
    // Refused, so the selection is still there, under the error.
    expect(alert.nextElementSibling).toBe(screen.getByText(/1 selected\./).closest(".banner"));
    expect(document.querySelector(".register-head [role=alert]")).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// Confirmation before a delete or a detach (#199)
// --------------------------------------------------------------------------- //

describe("the panel's Delete", () => {
  it("asks first, naming the row, and sends one delete on Yes", async () => {
    vi.mocked(api.del).mockReset();
    vi.mocked(api.del).mockResolvedValue(null);
    await mount({ open: "cinema" });
    const panel = await screen.findByRole("dialog");

    fireEvent.click(within(panel).getByRole("button", { name: "Delete" }));
    expect(api.del).not.toHaveBeenCalled();

    const confirm = screen.getByRole("dialog", { name: "Delete this transaction?" });
    expect(confirm.textContent).toContain("2026-03-24");
    expect(confirm.textContent).toContain("Cinema");
    expect(confirm.textContent).toContain("9.00");

    fireEvent.click(within(confirm).getByRole("button", { name: "Yes, delete it" }));
    await waitFor(() => expect(api.del).toHaveBeenCalledTimes(1));
    expect(api.del).toHaveBeenCalledWith("/transactions/cinema");
  });

  it("sends nothing when kept, and Escape closes only the question", async () => {
    vi.mocked(api.del).mockReset();
    await mount({ open: "cinema" });
    const panel = await screen.findByRole("dialog");

    fireEvent.click(within(panel).getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.queryByRole("dialog", { name: "Delete this transaction?" })).toBeNull();

    fireEvent.click(within(panel).getByRole("button", { name: "Delete" }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Delete this transaction?" })).toBeNull();
    // The panel underneath is still open.
    expect(screen.getByRole("dialog", { name: "2026-03-24 · Current" })).toBeTruthy();
    expect(api.del).not.toHaveBeenCalled();
  });
});

describe("the panel's Detach", () => {
  it("asks first, naming the file, and sends one detach on Yes", async () => {
    const receipt = {
      id: "rcpt-1",
      household_id: HOUSEHOLD.id,
      transaction_id: "cinema",
      content_sha256: "rcpt-1-sha",
      original_filename: "cinema.jpeg",
      media_type: "image/jpeg",
      byte_size: 2_400_000,
      width: 2000,
      height: 2600,
      page_count: null,
      captured_at: "2026-03-24T20:00:00Z",
      captured_at_is_local: false,
      gps_lat: null,
      gps_lon: null,
      gps_accuracy_m: null,
      gps_bearing: null,
      camera: "Fictional Handset 9",
      exif: null,
      client_encoded: false,
      note: null,
      uploaded_by_id: "user-1",
      uploaded_by_name: "Jane",
      created_at: "2026-03-24T21:00:00Z",
      download_name: "2026/2026-03-24-cinema.avif",
      has_original: false,
      download_bytes: 300_000,
      also_on: 0,
    };
    const base = vi.mocked(api.get).getMockImplementation()!;
    vi.mocked(api.get).mockImplementation(((path: string, options?: unknown) =>
      path === "/transactions/cinema/receipts"
        ? Promise.resolve([receipt])
        : base(path, options as never)) as typeof api.get);
    vi.mocked(api.patch).mockReset();
    vi.mocked(api.patch).mockResolvedValue({});
    await mount({ open: "cinema" });
    const panel = await screen.findByRole("dialog");

    fireEvent.click(await within(panel).findByRole("button", { name: "Detach" }));
    expect(api.patch).not.toHaveBeenCalled();
    const confirm = screen.getByRole("dialog", { name: "Detach this receipt?" });
    expect(confirm.textContent).toContain("2026-03-24-cinema.avif");

    fireEvent.click(within(confirm).getByRole("button", { name: "Yes, detach it" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(api.patch).toHaveBeenCalledWith("/receipts/rcpt-1", { detach: true });
  });
});
