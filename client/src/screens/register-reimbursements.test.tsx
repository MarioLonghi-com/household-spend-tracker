// @vitest-environment jsdom

/**
 * The register's work expenses: the filter, the grouped order, the W pill,
 * and the way in from the Reimbursements report.
 *
 * Checked by what reaches the server and by what lands on screen. The mock
 * answers each filter with different rows, so a query key that forgot the
 * filter would show the previous filter's rows -- which is the bug the key
 * is there to prevent, and a test asserting only the query string would pass
 * straight over it.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
import { Register, reimbursementChange, reimbursementSkippedNote } from "./Register";
import type { RegisterPreset } from "./Register";
import type { Household, Transaction } from "../lib/types";

afterEach(cleanup);

const HOUSEHOLD = { id: "house-1", name: "Ours", base_currency: "EUR" } as unknown as Household;

function row(
  over: Partial<Transaction> & { id: string; currency?: string },
): Transaction & { currency: string } {
  return {
    account_id: "acc-visa",
    date: "2026-09-12",
    amount: -2400,
    payee_id: null,
    payee_name: over.id,
    category_id: null,
    category_name: null,
    memo: null,
    cleared: "cleared",
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

/** Two payments, four repaid expenses in two currencies, one owed, one written off, one ordinary. */
const PAY_OCT = row({ id: "Employer Oct", date: "2026-10-30", amount: 2340, account_id: "acc-checking" });
const PAY_SEP = row({ id: "Employer Sep", date: "2026-09-30", amount: 32450, account_id: "acc-checking" });
const TAXI = row({ id: "Cabify", date: "2026-10-20", reimbursement: "expected", reimbursed_by_id: PAY_OCT.id });
const HOTEL = row({ id: "Hotel", date: "2026-09-12", amount: -24000, reimbursement: "expected", reimbursed_by_id: PAY_SEP.id });
const TRAIN = row({ id: "Renfe", date: "2026-09-03", amount: -8450, reimbursement: "expected", reimbursed_by_id: PAY_SEP.id });
/** In pounds, repaid in euros: linked, never compared. */
const LONDON = row({ id: "London hotel", date: "2026-10-22", amount: -15000, account_id: "acc-amex", currency: "GBP", reimbursement: "expected", reimbursed_by_id: PAY_OCT.id });
const OWED = row({ id: "Taxi owed", date: "2026-09-25", reimbursement: "expected" });
const OFF = row({ id: "Dinner", date: "2026-09-05", reimbursement: "written_off", currency: "GBP" });
const PLAIN = row({ id: "Bakery", date: "2026-09-26" });

/** What the server answers for each Work expenses view, newest first. */
const BY_VIEW: Record<string, Transaction[]> = {
  "": [PAY_OCT, LONDON, TAXI, PAY_SEP, PLAIN, OWED, HOTEL, OFF, TRAIN],
  work: [PAY_OCT, LONDON, TAXI, PAY_SEP, OWED, HOTEL, OFF, TRAIN],
  owed: [OWED],
  paid: [PAY_OCT, LONDON, TAXI, PAY_SEP, HOTEL, TRAIN],
  off: [OFF],
};

let asked: URLSearchParams[] = [];

beforeEach(() => {
  // The register remembers its filters (#143); each test starts from none.
  window.localStorage.clear();
  asked = [];
  vi.mocked(api.patch).mockReset();
  vi.mocked(api.post).mockReset();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => {
    if (path.includes("/transactions?")) {
      const params = new URLSearchParams(path.split("?")[1]);
      asked.push(params);
      const rows = BY_VIEW[params.get("reimbursement") ?? ""] ?? [];
      return Promise.resolve({ transactions: rows, total: rows.length, has_running_balance: false, capped: false });
    }
    if (path.endsWith("/reports/currencies")) return Promise.resolve({ currencies: ["EUR", "GBP"] });
    if (path.endsWith("/accounts"))
      return Promise.resolve([
        { id: "acc-visa", name: "Visa", currency: "EUR", type: "credit_card", country: null },
        { id: "acc-checking", name: "Checking", currency: "EUR", type: "checking", country: null },
        { id: "acc-amex", name: "Amex", currency: "GBP", type: "credit_card", country: null },
      ]);
    return Promise.resolve([]);
  }) as typeof api.get);
});

function mount(preset?: RegisterPreset) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Register household={HOUSEHOLD} preset={preset} />
    </QueryClientProvider>,
  );
}

const last = () => asked[asked.length - 1];
const workSelect = () => screen.getByRole("combobox", { name: "Work expenses" });

/** The payee of each drawn row, with a ↳ in front of the ones nested under a payment. */
function drawn(): string[] {
  return [...document.querySelectorAll("#register-rows tr")].map((tr) => {
    const nested = tr.querySelector(".work-child") ? "↳" : "";
    // By its label, not its place: a column added to the left (#143's
    // transfer mark) moved every index after it.
    const button = tr.querySelector('td[data-label="Payee"]');
    return nested + (button?.textContent ?? "").trim();
  });
}

describe("the Work expenses filter", () => {
  it("asks for each view, shows that view's rows, and Clear filters takes it off", async () => {
    mount();
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    expect(last().get("reimbursement")).toBeNull();

    fireEvent.change(workSelect(), { target: { value: "owed" } });
    await waitFor(() => expect(last().get("reimbursement")).toBe("owed"));
    await screen.findByText("Taxi owed");
    expect(screen.queryByText("Bakery")).toBeNull();

    // A different view is a different key: the written-off row replaces the
    // owed one rather than the owed one being served again from the cache.
    fireEvent.change(workSelect(), { target: { value: "off" } });
    await screen.findByText("Dinner");
    expect(screen.queryByText("Taxi owed")).toBeNull();
    expect(last().get("reimbursement")).toBe("off");

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(last().get("reimbursement")).toBeNull());
    await screen.findByText("Bakery");
    expect((workSelect() as HTMLSelectElement).value).toBe("");
  });

  it("offers the four views in words", async () => {
    mount();
    const labels = [...(workSelect() as HTMLSelectElement).options].map((one) => one.text);
    expect(labels).toEqual([
      "All transactions",
      "Work items and their repayments",
      "Not reimbursed yet",
      "Reimbursed, with the payment",
      "Written off",
    ]);
  });
});

describe("the grouped order", () => {
  it("puts each payment's expenses under it, oldest first, when payments are shown", async () => {
    mount();
    fireEvent.change(workSelect(), { target: { value: "paid" } });
    await screen.findByText("Renfe");
    expect(drawn()).toEqual([
      "Employer Oct",
      "↳Cabify",
      "↳London hotel",
      "Employer Sep",
      "↳Renfe",
      "↳Hotel",
    ]);
    expect(screen.getByText(/followed by the expenses it repaid/)).toBeTruthy();
  });

  it("leaves the server's order alone under a view with no payments in it", async () => {
    mount();
    await screen.findByText("Bakery");
    expect(drawn()).toEqual([
      "Employer Oct",
      "London hotel",
      "Cabify",
      "Employer Sep",
      "Bakery",
      "Taxi owed",
      "Hotel",
      "Dinner",
      "Renfe",
    ]);
  });
});

describe("the W pill", () => {
  it("says which of the three states a row is in, in words, and nothing on other rows", async () => {
    mount();
    await screen.findByText("Bakery");
    const pillOf = (payee: string) =>
      screen.getByText(payee).closest("tr")!.querySelector(".tag.work-owed, .tag.work-paid, .tag.work-off");

    expect(pillOf("Taxi owed")?.className).toBe("tag work-owed");
    expect(pillOf("Taxi owed")?.getAttribute("aria-label")).toBe("Work expense — not reimbursed yet");
    expect(pillOf("Hotel")?.className).toBe("tag work-paid");
    expect(pillOf("Hotel")?.getAttribute("title")).toBe("Work expense — reimbursed");
    expect(pillOf("Dinner")?.className).toBe("tag work-off");
    expect(pillOf("Bakery")).toBeNull();
    // A payment is not itself a work expense, so it carries no pill.
    expect(pillOf("Employer Sep")).toBeNull();
  });
});

describe("arriving from the Reimbursements report", () => {
  it("opens on the filter it was sent with, and on the row it was sent to", async () => {
    mount({ reimbursement: "owed", open: OWED.id });
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    expect(asked[0].get("reimbursement")).toBe("owed");
    expect((workSelect() as HTMLSelectElement).value).toBe("owed");
    const panel = await screen.findByRole("dialog");
    expect(panel.getAttribute("aria-label")).toBe("2026-09-25 · Visa");
  });
});

// --------------------------------------------------------------------------- //
// The side panel's Reimbursement block
// --------------------------------------------------------------------------- //

describe("what the panel's select sends", () => {
  const owed = { reimbursement: "expected", reimbursed_by_id: null } as const;
  const paid = { reimbursement: "expected", reimbursed_by_id: "pay" } as const;
  const plain = { reimbursement: null, reimbursed_by_id: null } as const;

  it("sends nothing when the choice is the value it already had", () => {
    expect(reimbursementChange(owed, "expected")).toBeNull();
    expect(reimbursementChange(plain, "")).toBeNull();
    expect(reimbursementChange({ reimbursement: "written_off", reimbursed_by_id: null }, "written_off")).toBeNull();
  });

  it("flags, writes off and clears", () => {
    expect(reimbursementChange(plain, "expected")).toEqual({ state: "expected" });
    expect(reimbursementChange(owed, "written_off")).toEqual({ state: "written_off" });
    expect(reimbursementChange(owed, "")).toEqual({ clear_state: true });
  });

  it("takes the link off in the same request when a repaid row is written off", () => {
    expect(reimbursementChange(paid, "written_off")).toEqual({
      state: "written_off",
      clear_settlement: true,
    });
  });
});

describe("the panel's Reimbursement block", () => {
  async function openRow(date: string) {
    mount();
    await screen.findByText("Bakery");
    fireEvent.click(screen.getByRole("button", { name: date }));
    return screen.findByRole("dialog");
  }

  it("flags an ordinary purchase, and sends nothing when the choice does not change", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...PLAIN, reimbursement: "expected" });
    await openRow(PLAIN.date);
    const select = screen.getByRole("combobox", { name: "Reimbursement" }) as HTMLSelectElement;
    expect(select.value).toBe("");

    fireEvent.change(select, { target: { value: "" } });
    expect(api.patch).not.toHaveBeenCalled();

    fireEvent.change(select, { target: { value: "expected" } });
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/transactions/Bakery/reimbursement", {
        state: "expected",
      }),
    );
    // The answer is what the block now shows: owed, and waiting.
    await screen.findByText(/Not reimbursed yet · \d+ days?/);
    expect(select.value).toBe("expected");
  });

  it("finds the payment among money in only, and links it", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...OWED, reimbursed_by_id: PAY_SEP.id });
    await openRow(OWED.date);
    fireEvent.click(screen.getByRole("button", { name: "Find the payment…" }));
    const picker = await screen.findByRole("dialog", { name: "Which payment repaid this?" });
    await waitFor(() => expect(picker.querySelectorAll("tbody tr").length).toBe(2));
    const offered = [...picker.querySelectorAll("tbody tr [data-primary]")].map((one) => one.textContent);
    expect(offered.sort()).toEqual(["Employer Oct", "Employer Sep"]);

    // ±45 days around the expense.
    const window = asked.find((one) => one.get("since") && !one.get("reimbursement"))!;
    expect(window.get("since")).toBe("2026-08-11");
    expect(window.get("until")).toBe("2026-11-09");

    const sep = [...picker.querySelectorAll("tbody tr")].find((tr) => tr.textContent?.includes("Employer Sep"))!;
    fireEvent.click(sep.querySelector("button")!);
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/transactions/Taxi owed/reimbursement", {
        settled_by_id: PAY_SEP.id,
      }),
    );
    await screen.findByText(/Reimbursed by/);
    expect(screen.queryByRole("dialog", { name: "Which payment repaid this?" })).toBeNull();
  });

  it("names the payment a repaid row points at, and can take the link off", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...HOTEL, reimbursed_by_id: null });
    const panel = await openRow(HOTEL.date);
    await waitFor(() => expect(panel.textContent).toContain("2026-09-30 · Checking ·"));
    expect(panel.textContent).toContain("· Employer Sep");

    fireEvent.click(screen.getByRole("button", { name: "Not reimbursed after all" }));
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith("/transactions/Hotel/reimbursement", {
        clear_settlement: true,
      }),
    );
    await screen.findByText(/Not reimbursed yet · /);
  });

  it("shows what a payment repaid, and that the figures agree", async () => {
    const panel = await openRow(PAY_SEP.date);
    await screen.findByText("Repays 2 expenses");
    const sum = panel.querySelector("dl.work-sum")!;
    const pairs = [...sum.querySelectorAll("dt")].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]);
    expect(pairs.map(([label]) => label)).toEqual(["Received", "Covered", "Difference"]);
    expect(pairs[0][1]).toBe(pairs[1][1]);
    expect(pairs[2][1]).toMatch(/0[.,]00/);
    expect(screen.queryByRole("combobox", { name: "Reimbursement" })).toBeNull();
  });

  it("does not compare a payment with expenses in another currency", async () => {
    const panel = await openRow(PAY_OCT.date);
    await screen.findByText("Repays 2 expenses");
    expect(panel.textContent).toContain("mixed currencies, not compared");
    expect(panel.querySelector("dl.work-sum")!.textContent).toMatch(/£/);
  });

  it("offers nothing on money in that nothing points at", async () => {
    const extra = row({ id: "Salary", date: "2026-09-28", amount: 300000, account_id: "acc-checking" });
    BY_VIEW[""] = [extra, ...BY_VIEW[""]];
    try {
      const panel = await openRow(extra.date);
      await waitFor(() => expect(asked.some((one) => one.get("reimbursement") === "paid")).toBe(true));
      expect(panel.textContent).not.toContain("Repays");
      expect(screen.queryByRole("combobox", { name: "Reimbursement" })).toBeNull();
    } finally {
      BY_VIEW[""] = BY_VIEW[""].filter((one) => one.id !== "Salary");
    }
  });
});

// --------------------------------------------------------------------------- //
// The bulk banner
// --------------------------------------------------------------------------- //

describe("the bulk banner", () => {
  const tick = (payee: string) =>
    fireEvent.click(
      screen.getByText(payee).closest("tr")!.querySelector('input[type="checkbox"]')!,
    );

  it("flags the selection in one act and says how many rows it left alone", async () => {
    vi.mocked(api.post).mockResolvedValue({
      transactions: [PLAIN, OWED, PAY_SEP],
      skipped_transfer_legs: 0,
      skipped_reimbursement: 1,
    });
    mount();
    await screen.findByText("Bakery");
    tick("Bakery");
    tick("Taxi owed");
    tick("Employer Sep");
    fireEvent.change(screen.getByRole("combobox", { name: "Set work expense on the selected rows" }), {
      target: { value: "expected" },
    });
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    const [path, body] = vi.mocked(api.post).mock.calls[0];
    expect(path).toBe("/households/house-1/transactions/bulk");
    expect(body).toMatchObject({ reimbursement: "expected" });
    expect(new Set((body as { transaction_ids: string[] }).transaction_ids)).toEqual(
      new Set(["Bakery", "Taxi owed", "Employer Sep"]),
    );
    await screen.findByText(/Changed 2 rows and left 1 alone/);
  });

  it("clears the flag with clear_reimbursement rather than a null", async () => {
    vi.mocked(api.post).mockResolvedValue({ transactions: [OWED], skipped_transfer_legs: 0, skipped_reimbursement: 0 });
    mount();
    await screen.findByText("Bakery");
    tick("Taxi owed");
    fireEvent.change(screen.getByRole("combobox", { name: "Set work expense on the selected rows" }), {
      target: { value: "__none" },
    });
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    expect(vi.mocked(api.post).mock.calls[0][1]).toMatchObject({ clear_reimbursement: true });
    expect(vi.mocked(api.post).mock.calls[0][1]).not.toHaveProperty("reimbursement");
  });

  it("links money out to the one payment in, and only for that shape", async () => {
    vi.mocked(api.post).mockResolvedValue([OWED, PLAIN]);
    mount();
    await screen.findByText("Bakery");
    const offered = () => screen.queryByRole("button", { name: "Link as reimbursement" });

    tick("Taxi owed");
    expect(offered()).toBeNull(); // no payment yet
    tick("Employer Sep");
    expect(offered()).not.toBeNull();
    tick("Employer Oct");
    expect(offered()).toBeNull(); // two payments: which one?
    tick("Employer Oct");
    tick("Bakery");
    // The per-currency sums stay on screen beside it.
    expect(document.querySelector(".selection-sums")?.textContent).toContain("EUR");

    fireEvent.click(offered()!);
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    const [path, body] = vi.mocked(api.post).mock.calls[0];
    expect(path).toBe("/households/house-1/transactions/reimbursements/link");
    expect((body as { settlement_id: string }).settlement_id).toBe(PAY_SEP.id);
    expect(new Set((body as { expense_ids: string[] }).expense_ids)).toEqual(new Set(["Taxi owed", "Bakery"]));
    await screen.findByText(/Linked 2 expenses to the payment of/);
  });

  it("does not offer the link when a transfer leg is selected", async () => {
    const leg = row({ id: "To savings", date: "2026-09-27", transfer_account_id: "acc-savings" });
    BY_VIEW[""] = [leg, ...BY_VIEW[""]];
    try {
      mount();
      await screen.findByText("To savings");
      tick("Taxi owed");
      tick("Employer Sep");
      expect(screen.queryByRole("button", { name: "Link as reimbursement" })).not.toBeNull();
      tick("To savings");
      expect(screen.queryByRole("button", { name: "Link as reimbursement" })).toBeNull();
    } finally {
      BY_VIEW[""] = BY_VIEW[""].filter((one) => one.id !== "To savings");
    }
  });

  it("says nothing when nothing was skipped", () => {
    expect(reimbursementSkippedNote(0, 4)).toBeNull();
    expect(reimbursementSkippedNote(1, 1)).toBe(
      "Changed 0 rows and left 1 alone — only money out that is not a transfer can be a work expense.",
    );
  });
});
