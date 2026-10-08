// @vitest-environment jsdom

/**
 * The Reimbursements report, handed a fixture.
 *
 * Every assertion is a value on screen: the four figures and the sentence
 * under each -- a line per ticked currency, never a sum -- the claim whose
 * currencies differ saying so instead of showing a difference, the row order
 * after a heading is clicked, the requests the currency boxes actually send,
 * and where a click sends somebody. Two currencies and two of each kind of
 * claim, so nothing passes because there was only one of something.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
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

import { api } from "../../lib/api";
import {
  Reimbursements,
  ReimbursementsBody,
  checkOptions,
  chooseCurrencies,
  isOrder,
  isRange,
  isTicked,
  mergeClaims,
} from "./Reimbursements";
import { REPORTS, Report, Reports, reportScreen } from "../Reports";
import { format } from "../../lib/money";
import { stickyKey, writeSticky } from "../../lib/sticky";
import type { Household, ReimbursementsReport } from "../../lib/types";

afterEach(() => {
  cleanup();
  // Table orders and ticks are remembered now; no test inherits another's.
  window.localStorage.clear();
});

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function aReport(over: Partial<ReimbursementsReport> = {}): ReimbursementsReport {
  return {
    currency: "EUR",
    since: null,
    until: null,
    available_currencies: ["EUR", "GBP"],
    outstanding: 2340 + 1200,
    outstanding_count: 2,
    oldest_outstanding: "2026-09-18",
    recovered: 32450,
    recovered_count: 2,
    written_off: 6000,
    written_off_count: 1,
    unmatched: 1500,
    outstanding_rows: [
      { id: "cabify", date: "2026-09-18", payee_name: "Cabify", account_id: "visa", account_name: "Visa", amount: 2340, has_receipt: false, memo: "Airport to the client", category_name: "Travel" },
      { id: "parking", date: "2026-09-22", payee_name: "Parking", account_id: "amex", account_name: "Amex", amount: 1200, has_receipt: true, memo: "Zone B, all day", category_name: "Travel" },
    ],
    claims: [
      {
        settlement: { id: "pay-oct", date: "2026-10-30", amount: 17200, currency: "EUR", account_id: "checking", account_name: "Checking", payee_name: "Employer Ltd", memo: "October expenses", category_name: "Salary" },
        expenses: [
          { id: "london", date: "2026-10-02", payee_name: "London hotel", account_id: "uk", account_name: "UK card", currency: "GBP", amount: 15000, memo: "London hotel, two nights", category_name: "Lodging" },
        ],
        covered: null,
        difference: null,
        currencies: ["EUR", "GBP"],
      },
      {
        settlement: { id: "pay-sep", date: "2026-09-30", amount: 33950, currency: "EUR", account_id: "checking", account_name: "Checking", payee_name: "Employer Ltd", memo: null, category_name: null },
        expenses: [
          { id: "train", date: "2026-09-03", payee_name: "Renfe", account_id: "visa", account_name: "Visa", currency: "EUR", amount: 8450, memo: "Madrid to Barcelona", category_name: "Travel" },
          { id: "hotel", date: "2026-09-12", payee_name: "Hotel", account_id: "visa", account_name: "Visa", currency: "EUR", amount: 24000, memo: null, category_name: "Lodging" },
        ],
        covered: 32450,
        difference: 1500,
        currencies: ["EUR"],
      },
    ],
    months: [
      { month: "2026-09", flagged: 41790, recovered: 32450, written_off: 6000, outstanding: 3540 },
      { month: "2026-10", flagged: 0, recovered: 0, written_off: 0, outstanding: 0 },
    ],
    ...over,
  };
}

/**
 * The pounds side of the same household: one owed expense, the mixed claim
 * the euro answer also lists (the server sends it under both), and a claim of
 * its own. Every GBP figure differs from its EUR one, and none is the sum.
 */
function aGbpReport(over: Partial<ReimbursementsReport> = {}): ReimbursementsReport {
  const euros = aReport();
  return aReport({
    currency: "GBP",
    outstanding: 900,
    outstanding_count: 1,
    oldest_outstanding: "2026-09-20",
    recovered: 15000 + 4000,
    recovered_count: 2,
    written_off: 0,
    written_off_count: 0,
    unmatched: 0,
    outstanding_rows: [
      { id: "tea", date: "2026-09-20", payee_name: "Tea", account_id: "uk", account_name: "UK card", amount: 900, has_receipt: false, memo: "Tea at Euston", category_name: null },
    ],
    claims: [
      euros.claims[0],
      {
        settlement: { id: "pay-gbp", date: "2026-10-05", amount: 4000, currency: "GBP", account_id: "uk", account_name: "UK card", payee_name: "Employer UK", memo: "Advance for Leeds", category_name: null },
        expenses: [
          { id: "leeds", date: "2026-09-25", payee_name: "Train to Leeds", account_id: "uk", account_name: "UK card", currency: "GBP", amount: 4000, memo: "Anytime return", category_name: "Travel" },
        ],
        covered: 4000,
        difference: 0,
        currencies: ["GBP"],
      },
    ],
    months: [{ month: "2026-09", flagged: 900 + 4000, recovered: 4000, written_off: 0, outstanding: 900 }],
    ...over,
  });
}

const eur = (minor: number) => format(minor, "EUR");
const gbp = (minor: number) => format(minor, "GBP");

function figure(label: string): HTMLElement {
  const found = [...document.querySelectorAll(".reimb-figure-label")].find(
    (one) => one.textContent === label,
  );
  return found!.closest(".reimb-figure") as HTMLElement;
}

describe("the report's figures and tables", () => {
  it("shows the four figures with the sentence under each", () => {
    render(<ReimbursementsBody household="house-1" reports={[aReport()]} onOpen={() => {}} asOf="2026-09-25" />);
    expect(figure("Work owes you").textContent).toContain(eur(3540));
    expect(figure("Work owes you").textContent).toContain("2 expenses · oldest 7 days");
    expect(figure("Work owes you").className).toContain("owed");
    expect(figure("Recovered").textContent).toContain(eur(32450));
    expect(figure("Recovered").textContent).toContain("2 expenses repaid");
    expect(figure("Written off").textContent).toContain(eur(6000));
    expect(figure("Paid, not matched").textContent).toContain(eur(1500));
    expect(figure("Paid, not matched").textContent).toContain("An advance not spent yet");
  });

  it("says a claim across two currencies is not compared, and adds up one that is", () => {
    render(<ReimbursementsBody household="house-1" reports={[aReport()]} onOpen={() => {}} asOf="2026-09-25" />);
    const claims = document.querySelectorAll("tr.reimb-claim");
    expect(claims.length).toBe(2);
    const [october, september] = [...claims];
    expect(october.textContent).toContain("mixed currencies, not compared");
    expect(october.textContent).toContain(format(15000, "GBP"));
    expect(september.textContent).not.toContain("not compared");
    expect(september.textContent).toContain(eur(32450));
    expect(september.textContent).toContain(eur(1500));
    // The expenses sit under their payment, oldest first, as the server sent them.
    const subRows = [...document.querySelectorAll("tr.reimb-claim-expense")].map((tr) => tr.textContent);
    expect(subRows[1]).toContain("Renfe");
    expect(subRows[2]).toContain("Hotel");
  });

  it("sorts the outstanding list at its headings, and its date goes to the register", () => {
    const open = vi.fn();
    render(<ReimbursementsBody household="house-1" reports={[aReport()]} onOpen={open} asOf="2026-09-25" />);
    const table = screen.getByRole("heading", { name: /Outstanding/ }).parentElement!;
    const payees = () =>
      [...table.querySelectorAll("tbody [data-primary]")].map((cell) => cell.firstChild?.textContent);
    expect(payees()).toEqual(["Cabify", "Parking"]); // oldest first
    fireEvent.click(within(table).getByRole("button", { name: /Owed/ }));
    // Money sorts largest first when its heading is first clicked.
    expect(payees()).toEqual(["Cabify", "Parking"]);
    fireEvent.click(within(table).getByRole("button", { name: /Owed/ }));
    expect(payees()).toEqual(["Parking", "Cabify"]);
    expect(within(table).getByText("3 days")).toBeTruthy();

    // The date is the way to the register, and only that: no dialog over it.
    fireEvent.click(within(table).getByRole("button", { name: "2026-09-22" }));
    expect(open).toHaveBeenCalledWith("parking");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says so when a currency has nothing outstanding and nothing linked", () => {
    render(
      <ReimbursementsBody
        household="house-1"
        reports={[aReport({ currency: "GBP", outstanding: 0, outstanding_count: 0, oldest_outstanding: null, outstanding_rows: [], claims: [], months: [] })]}
        onOpen={() => {}}
        asOf="2026-09-25"
      />,
    );
    expect(screen.getByText("Nothing outstanding in GBP.")).toBeTruthy();
    expect(screen.getByText("No payments linked yet.")).toBeTruthy();
    expect(screen.getByText("No work expenses in GBP yet.")).toBeTruthy();
    expect(figure("Work owes you").textContent).toContain("Nothing owed");
    expect(figure("Work owes you").className).not.toContain("owed");
  });
});

describe("two currencies ticked at once", () => {
  const both = () => [aReport(), aGbpReport()];

  it("gives each figure a line per currency and never adds them up", () => {
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={() => {}} asOf="2026-09-25" />);
    const lines = (label: string) =>
      [...figure(label).querySelectorAll(".reimb-figure-line")].map((one) => one.textContent);
    expect(lines("Work owes you")).toEqual([
      `${eur(3540)}2 expenses · oldest 7 days`,
      `${gbp(900)}1 expense · oldest 5 days`,
    ]);
    expect(lines("Recovered")).toEqual([`${eur(32450)}2 expenses repaid`, `${gbp(19000)}2 expenses repaid`]);
    expect(lines("Written off")[1]).toBe(`${gbp(0)}None`);
    expect(lines("Paid, not matched")).toEqual([
      `${eur(1500)}An advance not spent yet, or an overpayment`,
      `${gbp(0)}Every payment is accounted for`,
    ]);
    // Neither sum is anywhere: 35.40 + 9.00, in either symbol.
    for (const sum of [eur(4440), gbp(4440), eur(51450), gbp(51450)])
      expect(document.body.textContent).not.toContain(sum);
    // Only the lines that owe wear the warning; both do here.
    expect(figure("Work owes you").querySelectorAll(".reimb-figure-line.owed").length).toBe(2);
  });

  it("merges the outstanding lists, each amount in its own currency, money sorted currency first", () => {
    render(
      <ReimbursementsBody household="house-1" reports={both()} baseCurrency="EUR" onOpen={() => {}} asOf="2026-09-25" />,
    );
    const table = screen.getByRole("heading", { name: /Outstanding/ }).parentElement!;
    const rows = () =>
      [...table.querySelectorAll("tbody tr")].map((tr) => [
        tr.querySelector("[data-primary]")!.firstChild?.textContent,
        tr.querySelector("[data-figure]")!.textContent,
      ]);
    expect(rows()).toEqual([
      ["Cabify", eur(2340)],
      ["Tea", gbp(900)],
      ["Parking", eur(1200)],
    ]); // oldest first, across both
    fireEvent.click(within(table).getByRole("button", { name: /Owed/ }));
    // Largest first -- inside each currency, euros (the base) before pounds,
    // and the same group order when the column turns round.
    expect(rows().map((one) => one[0])).toEqual(["Cabify", "Parking", "Tea"]);
    fireEvent.click(within(table).getByRole("button", { name: /Owed/ }));
    expect(rows().map((one) => one[0])).toEqual(["Parking", "Cabify", "Tea"]);
  });

  it("lists a claim both currencies share once, among each currency's own", () => {
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={() => {}} asOf="2026-09-25" />);
    const payments = [...document.querySelectorAll("tr.reimb-claim")].map(
      (tr) => tr.querySelector("td")!.textContent,
    );
    // Newest payment first, and the October one the two answers share once.
    expect(payments).toEqual(["2026-10-30", "2026-10-05", "2026-09-30"]);
    expect(mergeClaims(both()).map((one) => one.settlement.id)).toEqual(["pay-oct", "pay-gbp", "pay-sep"]);
    expect(document.querySelectorAll("tr.reimb-claim")[1].textContent).toContain(gbp(4000));
  });

  it("gives a month a row per currency, named, and says which ones had nothing", () => {
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={() => {}} asOf="2026-09-25" />);
    const months = screen.getByRole("heading", { name: /By month/ }).parentElement!;
    const first = [...months.querySelectorAll("tbody tr")].map((tr) => tr.firstElementChild!.textContent);
    expect(first).toEqual(["Sep 2026 · EUR", "Sep 2026 · GBP", "Oct 2026 · EUR"]);
    cleanup();
    const empty = (currency: string) =>
      aReport({ currency, outstanding: 0, outstanding_count: 0, oldest_outstanding: null, outstanding_rows: [], months: [] });
    render(<ReimbursementsBody household="house-1" reports={[empty("EUR"), empty("GBP")]} onOpen={() => {}} />);
    expect(screen.getByText("Nothing outstanding in EUR or GBP.")).toBeTruthy();
    expect(screen.getByText("No work expenses in EUR or GBP yet.")).toBeTruthy();
  });
});

describe("the report as a screen", () => {
  let asked: URLSearchParams[] = [];
  let answer: (currency: string) => ReimbursementsReport;

  beforeEach(() => {
    asked = [];
    answer = (currency) => aReport({ currency });
    vi.mocked(api.get).mockReset();
    vi.mocked(api.get).mockImplementation(((path: string) => {
      if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP", "USD"] });
      if (path.includes("/reports/reimbursements?")) {
        const params = new URLSearchParams(path.split("?")[1]);
        asked.push(params);
        return Promise.resolve(answer(params.get("currency")!));
      }
      return Promise.resolve([]);
    }) as typeof api.get);
  });

  function mount(onOpenRegister = vi.fn()) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Reimbursements household={HOUSEHOLD} onOpenRegister={onOpenRegister} />
      </QueryClientProvider>,
    );
    return onOpenRegister;
  }

  const boxes = () => within(screen.getByRole("group", { name: "Currency" })).getAllByRole("checkbox");
  const box = (code: string) =>
    within(screen.getByRole("group", { name: "Currency" })).getByRole("checkbox", { name: code });

  it("opens on the busiest currency and asks for a second one when it is ticked", async () => {
    mount();
    await screen.findByText("Work owes you");
    expect(asked.map((one) => one.get("currency"))).toEqual(["EUR"]);
    // Only the currencies with work money in them: USD has none.
    expect(boxes().map((one) => one.parentElement!.textContent)).toEqual(["EUR", "GBP"]);
    expect(boxes().map((one) => (one as HTMLInputElement).checked)).toEqual([true, false]);
    // The only ticked box cannot be unticked: a report of no currency is blank.
    expect((box("EUR") as HTMLInputElement).disabled).toBe(true);

    fireEvent.click(box("GBP"));
    await waitFor(() => expect(asked.map((one) => one.get("currency"))).toEqual(["EUR", "GBP"]));
    await waitFor(() => expect(figure("Work owes you").querySelectorAll(".reimb-figure-line").length).toBe(2));
    expect((box("EUR") as HTMLInputElement).disabled).toBe(false);

    // Unticking EUR leaves GBP alone on screen, without asking again.
    fireEvent.click(box("EUR"));
    await waitFor(() => expect(figure("Work owes you").querySelectorAll(".reimb-figure-line").length).toBe(1));
    // This test's server answers every currency with the same figures.
    expect(figure("Work owes you").textContent).toContain(gbp(3540));
    expect(figure("Work owes you").textContent).not.toContain(eur(3540));
    expect(asked.length).toBe(2);
  });

  it("moves to the currency the claims are in when the busiest one has none", async () => {
    answer = (currency) => aReport({ currency, available_currencies: ["GBP"] });
    mount();
    await waitFor(() => expect(asked.map((one) => one.get("currency"))).toEqual(["EUR", "GBP"]));
  });

  it("says how to start when nothing is flagged anywhere", async () => {
    answer = (currency) =>
      aReport({ currency, available_currencies: [], outstanding_rows: [], claims: [], months: [] });
    mount();
    await screen.findByText(/Nothing is marked as a work expense yet/);
    expect(screen.queryByText("Work owes you")).toBeNull();
  });

  it("sends somebody to the register with the owed view on, or with one row open", async () => {
    const go = mount();
    await screen.findByText("Cabify");
    fireEvent.click(screen.getByRole("button", { name: "Show in Transactions" }));
    expect(go).toHaveBeenLastCalledWith({ reimbursement: "owed" });
    fireEvent.click(screen.getByRole("button", { name: "2026-09-18" }));
    expect(go).toHaveBeenLastCalledWith({ reimbursement: "owed", open: "cabify" });
  });
});

describe("the Memo column (#142)", () => {
  const both = () => [aReport(), aGbpReport()];

  it("shows each owed row's memo, in both currencies, and sorts by it", () => {
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={() => {}} asOf="2026-09-25" />);
    const table = screen.getByRole("heading", { name: /Outstanding/ }).parentElement!;
    const memos = () => [...table.querySelectorAll('tbody td[data-label="Memo"]')].map((td) => td.textContent);
    expect(memos()).toEqual(["Airport to the client", "Tea at Euston", "Zone B, all day"]);
    fireEvent.click(within(table).getByRole("button", { name: /Memo/ }));
    expect(memos()).toEqual(["Airport to the client", "Tea at Euston", "Zone B, all day"]);
    fireEvent.click(within(table).getByRole("button", { name: /Memo/ }));
    expect(memos()).toEqual(["Zone B, all day", "Tea at Euston", "Airport to the client"]);
  });

  it("shows a payment's memo on its row and each expense's on its own, sorting by the payment's", () => {
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={() => {}} asOf="2026-09-25" />);
    const table = screen.getByRole("heading", { name: /Payments from work/ }).parentElement!;
    const payments = () =>
      [...table.querySelectorAll("tr.reimb-claim")].map((tr) => tr.querySelector(".reimb-memo")!.textContent);
    expect(payments()).toEqual(["October expenses", "Advance for Leeds", ""]);
    const expenses = [...table.querySelectorAll("tr.reimb-claim-expense .reimb-memo")].map((td) => td.textContent);
    expect(expenses).toEqual(["London hotel, two nights", "Anytime return", "Madrid to Barcelona", ""]);

    fireEvent.click(within(table).getByRole("button", { name: /Memo/ }));
    // A to Z, the payment with no memo last -- and its expenses still under it.
    expect(payments()).toEqual(["Advance for Leeds", "October expenses", ""]);
    const rows = [...table.querySelectorAll("tbody tr")].map((tr) =>
      tr.classList.contains("reimb-claim") ? "payment" : "expense",
    );
    expect(rows).toEqual(["payment", "expense", "payment", "expense", "payment", "expense", "expense"]);
  });
});

describe("a row's details (#142)", () => {
  const both = () => [aReport(), aGbpReport()];
  const detail = (dialog: HTMLElement) =>
    Object.fromEntries(
      [...dialog.querySelectorAll("dt")].map((dt) => [dt.textContent, dt.nextElementSibling!.textContent]),
    );

  it("opens an owed expense's details on a click, and goes to the register from there", () => {
    const open = vi.fn();
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={open} asOf="2026-09-25" />);
    fireEvent.click(screen.getByText("Zone B, all day"));
    const dialog = screen.getByRole("dialog", { name: "Parking · 2026-09-22" });
    expect(detail(dialog)).toEqual({
      Date: "2026-09-22",
      Account: "Amex",
      Payee: "Parking",
      Memo: "Zone B, all day",
      Category: "Travel",
      Spent: eur(1200),
      Reimbursement: "Work should pay this back — not repaid yet, 3 days waiting",
      "Linked to": "No payment linked yet",
    });
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Open in Transactions" }));
    expect(open).toHaveBeenCalledWith("parking", "owed");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens from the keyboard on the row, not on the date button inside it, and Escape closes it", () => {
    const open = vi.fn();
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={open} asOf="2026-09-25" />);
    const tea = screen.getByText("Tea at Euston").closest("tr")!;
    expect(tea.tabIndex).toBe(0);
    // Enter on the date button is the button's; the row must not answer it too.
    fireEvent.keyDown(within(tea).getByRole("button", { name: "2026-09-20" }), { key: "Enter" });
    expect(screen.queryByRole("dialog")).toBeNull();

    tea.focus();
    fireEvent.keyDown(tea, { key: "Enter" });
    const dialog = screen.getByRole("dialog", { name: "Tea · 2026-09-20" });
    expect(detail(dialog).Spent).toBe(gbp(900));
    expect(detail(dialog).Category).toBe("—");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(tea, { key: " " });
    expect(screen.getByRole("dialog", { name: "Tea · 2026-09-20" })).toBeTruthy();
  });

  it("says what a payment repaid, and which payment repaid an expense, across currencies", () => {
    const open = vi.fn();
    render(<ReimbursementsBody household="house-1" reports={both()} onOpen={open} asOf="2026-09-25" />);
    fireEvent.click(screen.getByText("October expenses"));
    let dialog = screen.getByRole("dialog", { name: "Employer Ltd · 2026-10-30" });
    expect(detail(dialog)).toMatchObject({
      Memo: "October expenses",
      Category: "Salary",
      Received: eur(17200),
      Reimbursement: "A payment from work",
    });
    expect(detail(dialog).Repaid).toContain(`2026-10-02 · London hotel · ${gbp(15000)}`);
    expect(detail(dialog).Repaid).toContain("mixed currencies, not compared");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));

    fireEvent.click(screen.getByText("London hotel, two nights"));
    dialog = screen.getByRole("dialog", { name: "London hotel · 2026-10-02" });
    expect(detail(dialog)).toMatchObject({
      Account: "UK card",
      Category: "Lodging",
      Spent: gbp(15000),
      Reimbursement: "Work should pay this back — repaid",
      "Linked to": `Repaid by 2026-10-30 · Employer Ltd · ${eur(17200)}`,
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Open in Transactions" }));
    expect(open).toHaveBeenCalledWith("london", "paid");

    // The September payment repaid two: each expense says it had company.
    fireEvent.click(screen.getByText("Madrid to Barcelona"));
    expect(detail(screen.getByRole("dialog"))["Linked to"]).toBe(
      `Repaid by 2026-09-30 · Employer Ltd · ${eur(33950)} with 1 other expense`,
    );
  });
});

describe("what the screen remembers (#142)", () => {
  const key = (name: string, household = "house-1") => stickyKey("reimbursements", household, name);
  /** What is stored, as stored: not through a check, which is what is under test. */
  const stored = (name: string): unknown => JSON.parse(window.localStorage.getItem(key(name)) ?? "null");
  let asked: URLSearchParams[] = [];

  beforeEach(() => {
    asked = [];
    vi.mocked(api.get).mockReset();
    vi.mocked(api.get).mockImplementation(((path: string) => {
      if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP", "USD"] });
      if (path.includes("/reports/reimbursements?")) {
        const params = new URLSearchParams(path.split("?")[1]);
        asked.push(params);
        const code = params.get("currency")!;
        return Promise.resolve(code === "GBP" ? aGbpReport() : aReport({ currency: code }));
      }
      return Promise.resolve([]);
    }) as typeof api.get);
  });

  function mount() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Reimbursements household={HOUSEHOLD} />
      </QueryClientProvider>,
    );
  }
  const ticked = () =>
    within(screen.getByRole("group", { name: "Currency" }))
      .getAllByRole("checkbox")
      .filter((one) => (one as HTMLInputElement).checked)
      .map((one) => one.parentElement!.textContent);

  it("remembers the ticks, and comes back to them and to the dates last left", async () => {
    mount();
    await screen.findByText("Work owes you");
    fireEvent.click(screen.getByRole("checkbox", { name: "GBP" }));
    await waitFor(() => expect(stored("currencies")).toEqual(["EUR", "GBP"]));
    // Another household's settings are its own.
    expect(window.localStorage.getItem(key("currencies", "house-2"))).toBeNull();

    cleanup();
    asked = [];
    writeSticky(key("range"), { since: "2026-09-01", until: "2026-09-30" });
    mount();
    await waitFor(() => expect(figure("Work owes you").querySelectorAll(".reimb-figure-line").length).toBe(2));
    expect(ticked()).toEqual(["EUR", "GBP"]);
    expect(asked.map((one) => one.get("currency")).sort()).toEqual(["EUR", "GBP"]);
    for (const one of asked) {
      expect(one.get("since")).toBe("2026-09-01");
      expect(one.get("until")).toBe("2026-09-30");
    }
  });

  it("drops a remembered currency with no work money now, without a word", async () => {
    writeSticky(key("currencies"), ["USD", "GBP"]);
    mount();
    await waitFor(() => expect(ticked()).toEqual(["GBP"]));
    await waitFor(() => expect(figure("Work owes you").textContent).toContain(gbp(900)));
    expect(screen.queryByRole("checkbox", { name: "USD" })).toBeNull();
    expect(figure("Work owes you").querySelectorAll(".reimb-figure-line").length).toBe(1);
  });

  it("reads a damaged setting as the default rather than asking for it", async () => {
    window.localStorage.setItem(key("currencies"), JSON.stringify("GBP")); // a code, not a list
    window.localStorage.setItem(key("range"), JSON.stringify({ since: "last week", until: null }));
    mount();
    await screen.findByText("Work owes you");
    expect(asked.map((one) => [one.get("currency"), one.get("since")])).toEqual([["EUR", null]]);
    expect(ticked()).toEqual(["EUR"]);
  });

  it("remembers each table's order, column and direction together", () => {
    render(<ReimbursementsBody household="house-1" reports={[aReport()]} onOpen={() => {}} asOf="2026-09-25" />);
    const outstanding = () => screen.getByRole("heading", { name: /Outstanding/ }).parentElement!;
    fireEvent.click(within(outstanding()).getByRole("button", { name: /Owed/ }));
    fireEvent.click(within(outstanding()).getByRole("button", { name: /Owed/ }));
    expect(stored("outstanding.order")).toEqual({ sort: "amount", direction: "asc" });
    const months = screen.getByRole("heading", { name: /By month/ }).parentElement!;
    fireEvent.click(within(months).getByRole("button", { name: /Month/ }));
    expect(stored("months.order")).toEqual({ sort: "month", direction: "desc" });

    cleanup();
    render(<ReimbursementsBody household="house-1" reports={[aReport()]} onOpen={() => {}} asOf="2026-09-25" />);
    const payees = [...outstanding().querySelectorAll("tbody [data-primary]")].map(
      (cell) => cell.firstChild?.textContent,
    );
    expect(payees).toEqual(["Parking", "Cabify"]); // smallest first, as left
    expect(within(outstanding()).getByRole("columnheader", { name: /Owed/ }).getAttribute("aria-sort")).toBe(
      "ascending",
    );
    // The other household opens on the default: oldest first.
    cleanup();
    render(<ReimbursementsBody household="house-2" reports={[aReport()]} onOpen={() => {}} asOf="2026-09-25" />);
    expect(
      [...outstanding().querySelectorAll("tbody [data-primary]")].map((cell) => cell.firstChild?.textContent),
    ).toEqual(["Cabify", "Parking"]);
  });

  it("checks what it reads back", () => {
    expect(isTicked(null)).toBe(true);
    expect(isTicked(["EUR", "GBP"])).toBe(true);
    expect(isTicked(["EUR", 4])).toBe(false);
    expect(isTicked(["euro"])).toBe(false);
    expect(isTicked("EUR")).toBe(false);
    expect(isRange({ since: "2026-09-01", until: null })).toBe(true);
    expect(isRange({ since: "yesterday", until: null })).toBe(false);
    expect(isRange({ since: null })).toBe(false);
    expect(isRange([])).toBe(false);
    const order = isOrder(["date", "amount"] as const);
    expect(order({ sort: "amount", direction: "desc" })).toBe(true);
    expect(order({ sort: "memo", direction: "desc" })).toBe(false);
    expect(order({ sort: "date", direction: "up" })).toBe(false);
    expect(order(null)).toBe(false);
  });
});

describe("where the report is listed", () => {
  it("is on the Reports index, which is also what the menu is built from", () => {
    const entry = REPORTS.find((one) => one.key === "reimbursements");
    expect(entry?.label).toBe("Reimbursements");
    expect(reportScreen("reimbursements")).toBe("report-reimbursements");
  });

  it("keeps the currencies on screen in the boxes even when the list does not name them", () => {
    expect(checkOptions(["GBP"], ["EUR"])).toEqual(["GBP", "EUR"]);
    expect(checkOptions(["EUR", "GBP"], ["GBP"])).toEqual(["EUR", "GBP"]);
  });

  it("asks for what was ticked, busiest first, and drops what has no work money now", () => {
    // Before the first answer: what was ticked, or the household's busiest.
    expect(chooseCurrencies(null, undefined, "EUR")).toEqual(["EUR"]);
    expect(chooseCurrencies(["GBP", "USD"], undefined, "EUR")).toEqual(["GBP", "USD"]);
    // After it: in the report's order, whatever order they were ticked in.
    expect(chooseCurrencies(["GBP", "EUR"], ["EUR", "GBP"], "EUR")).toEqual(["EUR", "GBP"]);
    // USD has no work money any more: dropped, quietly.
    expect(chooseCurrencies(["USD", "GBP"], ["EUR", "GBP"], "EUR")).toEqual(["GBP"]);
    // Nothing ticked is left: the busiest with work money, as on a first visit.
    expect(chooseCurrencies(["USD"], ["GBP", "EUR"], "EUR")).toEqual(["GBP"]);
    expect(chooseCurrencies(null, ["GBP", "EUR"], "EUR")).toEqual(["GBP"]);
    // Nothing flagged anywhere: the guess, so the screen can say so.
    expect(chooseCurrencies(null, [], "EUR")).toEqual(["EUR"]);
  });
});

describe("in en-XA, the report shows no English", () => {
  /** The fixtures' own words: payees, accounts, memos and categories are data. */
  function fixtureWords(reports: ReimbursementsReport[]): Set<string> {
    const said: (string | null)[] = [];
    for (const one of reports) {
      for (const row of one.outstanding_rows) said.push(row.payee_name, row.account_name, row.memo, row.category_name);
      for (const claim of one.claims)
        for (const row of [claim.settlement, ...claim.expenses])
          said.push(row.payee_name, row.account_name, row.memo, row.category_name);
    }
    return new Set(said.flatMap((text) => (text ?? "").split(/[\s,]+/)).concat(["Ours"]));
  }

  it("the figures, the three tables, every kind of detail, and the index", async () => {
    const { activate } = await import("../../lib/i18n");
    const { untranslated } = await import("../../test-pseudo");
    await activate("en-XA");
    try {
      const reports = [aReport(), aGbpReport()];
      const data = fixtureWords(reports);
      const left = () => untranslated(document.body).filter((word) => !data.has(word));
      render(<ReimbursementsBody household="house-1" reports={reports} onOpen={() => {}} asOf="2026-09-25" />);
      expect(left()).toEqual([]);

      // An owed row, a payment, and an expense a payment repaid.
      for (const pick of [
        () => screen.getByText("Cabify").closest("tr")!,
        () => document.querySelector<HTMLElement>("tr.reimb-claim")!,
        () => document.querySelector<HTMLElement>("tr.reimb-claim-expense")!,
      ]) {
        fireEvent.click(pick());
        await screen.findByRole("dialog");
        expect(left()).toEqual([]);
        fireEvent.keyDown(document, { key: "Escape" });
        cleanup();
        render(<ReimbursementsBody household="house-1" reports={reports} onOpen={() => {}} asOf="2026-09-25" />);
      }
      cleanup();

      // Nothing in either table, and the index of reports with one open.
      render(
        <ReimbursementsBody
          household="house-1"
          reports={[aReport({ outstanding_rows: [], outstanding_count: 0, claims: [], months: [], written_off_count: 0, unmatched: 0 })]}
          onOpen={() => {}}
        />,
      );
      expect(left()).toEqual([]);
      cleanup();
      render(<Reports household={HOUSEHOLD} onOpen={() => {}} />);
      expect(left()).toEqual([]);
    } finally {
      await activate("en");
    }
  });

  it("the currency checks and their hint, and the screen with nothing flagged", async () => {
    const { activate } = await import("../../lib/i18n");
    const { untranslated } = await import("../../test-pseudo");
    await activate("en-XA");
    try {
      vi.mocked(api.get).mockImplementation(((path: string) => {
        if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP"] });
        return Promise.resolve(aReport({ available_currencies: [] }));
      }) as typeof api.get);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <Report household={HOUSEHOLD} screen={reportScreen("reimbursements")} onBack={() => {}} onOpenRegister={() => {}} />
        </QueryClientProvider>,
      );
      // Nothing flagged yet: the empty state.
      await waitFor(() => expect(document.querySelector(".empty")).not.toBeNull());
      expect(untranslated(document.body).filter((word) => word !== "Ours")).toEqual([]);
      cleanup();

      // Something flagged in two currencies: the checks, and the hint beside them.
      vi.mocked(api.get).mockImplementation(((path: string) => {
        if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP"] });
        return Promise.resolve(aReport());
      }) as typeof api.get);
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <Report household={HOUSEHOLD} screen={reportScreen("reimbursements")} onBack={() => {}} onOpenRegister={() => {}} />
        </QueryClientProvider>,
      );
      await screen.findByText("Cabify");
      expect(document.querySelectorAll(".reimb-currencies input")).toHaveLength(2);
      fireEvent.click(document.querySelector<HTMLButtonElement>(".reimb-currencies .hint-open")!);
      const data = fixtureWords([aReport()]);
      expect(untranslated(document.body).filter((word) => word !== "Ours" && !data.has(word))).toEqual([]);
    } finally {
      await activate("en");
    }
  });
});

