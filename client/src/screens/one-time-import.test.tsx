// @vitest-environment jsdom

/**
 * One-time Import · YNAB (#183), from the bottom of the household page.
 *
 * The server decides everything about the rows; what is pinned here is the
 * wizard around it: where the section sits and where it points for more
 * workflows, the two notes a user reads before handing anything over, that an
 * account can take only one YNAB account, that YNAB's own buckets stay
 * uncategorised, that nothing is previewed until the cleared-reset is
 * acknowledged, that "OK for all" reaches the commit, and that the report's
 * .txt is the server's text under a dated name. Every call resends the source.
 *
 * All names and figures below are made up.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "../lib/api";
import type { Household, User } from "../lib/types";
import { HouseholdPage } from "./Household";
import { money } from "./ynab/mapping";
import type { Analysis, ImportPlan, ImportReport } from "./ynab/types";
import { RULES_LISTED } from "./ynab/results";

const HOME: Household = {
  id: "hh-home",
  name: "Home",
  base_currency: "GBP",
  date_format: "YYYY-MM-DD",
  note: null,
  theme: "default",
  accent: null,
  colours: null,
  receipts_keep_original: false,
  receipts_keep_original_forced: false,
  receipts_with_original: 0,
};

const USER: User = {
  id: "u-1",
  email: "someone@example.test",
  display_name: "Someone",
  role: "owner",
  disabled_at: null,
};

function analysis(extra: Partial<Analysis> = {}): Analysis {
  return {
    source: { via: "csv", filename: "Test Plan as of 2026-01-01 Register.csv", plan_name: null },
    currency: { detected: "GBP", symbol: "£", confirmed_needed: false },
    date_format: { detected: "YYYY-MM-DD", ambiguous: false, options: ["YYYY-MM-DD"] },
    totals: {
      rows: 40,
      date_min: "2025-01-02",
      date_max: "2025-12-30",
      transfers: 6,
      splits: 2,
      starting_balance_rows: 2,
      cleared: { reconciled: 20, cleared: 15, uncleared: 5 },
    },
    flags: [
      { label: "Orange - Check", count: 3 },
      { label: "Blue", count: 1 },
    ],
    accounts: [
      {
        key: "Alpha Current",
        name: "Alpha Current",
        rows: 25,
        date_min: "2025-01-02",
        date_max: "2025-12-30",
        balance_minor: 123456,
        type_hint: "checking",
        closed: false,
        suggestion: { kind: "existing", account_id: "acc-1", score: 0.9 },
      },
      {
        key: "Beta Card",
        name: "Beta Card",
        rows: 15,
        date_min: "2025-02-01",
        date_max: "2025-11-30",
        balance_minor: -4500,
        type_hint: "credit_card",
        closed: false,
        suggestion: { kind: "create", name: "Beta Card", type: "credit_card" },
      },
    ],
    categories: [
      {
        key: "Groceries",
        name: "Groceries",
        groups: ["Everyday"],
        rows: 12,
        fixed_uncategorised: false,
        suggestion: { kind: "existing", category_id: "cat-1", score: 0.95 },
      },
      {
        key: "Inflow: Ready to Assign",
        name: "Inflow: Ready to Assign",
        groups: ["Inflow"],
        rows: 4,
        fixed_uncategorised: true,
        suggestion: { kind: "uncategorised" },
      },
    ],
    targets: {
      accounts: [
        {
          id: "acc-1",
          name: "Test Current",
          type: "checking",
          currency: "GBP",
          institution: "Example Bank",
          identifiers_masked: ["…1234"],
          closed: false,
          balance_minor: 50000,
          txn_count: 10,
          first_date: "2024-06-01",
          last_date: "2025-12-01",
          eligible: true,
        },
        {
          id: "acc-2",
          name: "Test Savings",
          type: "savings",
          currency: "GBP",
          institution: null,
          identifiers_masked: [],
          closed: true,
          balance_minor: 0,
          txn_count: 0,
          first_date: null,
          last_date: null,
          eligible: true,
        },
        {
          id: "acc-3",
          name: "Test Euro",
          type: "checking",
          currency: "EUR",
          institution: null,
          identifiers_masked: [],
          closed: false,
          balance_minor: 100,
          txn_count: 1,
          first_date: "2025-01-01",
          last_date: "2025-01-01",
          eligible: false,
        },
      ],
      categories: [
        { id: "cat-1", name: "Food", group_name: "Living", archived: false },
        { id: "cat-2", name: "Fun", group_name: "Extras", archived: false },
      ],
    },
    previous_imports: [],
    ...extra,
  };
}

function report(extra: Partial<ImportReport> = {}): ImportReport {
  return {
    committed: false,
    batch_id: null,
    counts: {
      rows_in_file: 40,
      imported: 36,
      transfers_linked: 3,
      duplicates_skipped: 2,
      duplicates_imported: 0,
      skipped_account: 0,
      skipped_date_range: 0,
      skipped_starting_balance: 0,
      failed: 1,
    },
    created: { accounts: [], categories: [], payees: 0 },
    duplicates: [],
    not_imported: [
      {
        row_ref: "r-9",
        date: "2025-03-03",
        date_text: "2025-03-03",
        account: "Alpha Current",
        payee: "Test Shop",
        category: "Groceries",
        memo: null,
        amount_minor: -999,
        reason: "bad amount",
      },
      {
        row_ref: "r-8",
        date: null,
        date_text: "2025-13-45",
        account: "Beta Card",
        payee: "Test Kiosk",
        category: null,
        memo: null,
        amount_minor: -100,
        reason: "'2025-13-45' is not a YYYY-MM-DD date",
      },
    ],
    unpaired_transfers: [
      {
        row_ref: "r-7",
        date: "2025-06-06",
        date_text: "2025-06-06",
        account: "Alpha Current",
        payee: "Transfer : Skipped Pot",
        category: null,
        memo: null,
        amount_minor: -2500,
        reason: "the other side's account is skipped",
      },
    ],
    bank_text_rows: 0,
    report_text: "Not imported\n2025-03-03 Alpha Current Test Shop -9.99 bad amount\n",
    ...extra,
  };
}

const DUPLICATES: ImportReport["duplicates"] = [
  {
    row_ref: "r-1",
    date: "2025-04-01",
    account: "Alpha Current",
    payee: "Test Cafe",
    amount_minor: -350,
    memo: null,
    existing: { id: "t-1", date: "2025-04-02", payee: "Test Cafe", amount_minor: -350 },
    decision: "pending",
  },
  {
    row_ref: "r-2",
    date: "2025-05-10",
    account: "Beta Card",
    payee: "Test Store",
    amount_minor: -1200,
    memo: "gift",
    existing: { id: "t-2", date: "2025-05-10", payee: "Test Store", amount_minor: -1200 },
    decision: "pending",
  },
];

let found: Analysis;
let dryRun: ImportReport;
let kept: ImportReport;

beforeEach(() => {
  found = analysis();
  dryRun = report();
  kept = report({ committed: true, batch_id: "b-1" });
  // The page's other sections never answer, which keeps them on their loading
  // line and out of the way.
  vi.mocked(api.get).mockImplementation((path: string) =>
    path === "/themes" ? Promise.resolve([]) : new Promise(() => {}),
  );
  vi.mocked(api.upload).mockImplementation(async (path: string) => {
    if (path.endsWith("/analyse")) return found as never;
    if (path.endsWith("/preview")) return dryRun as never;
    if (path.endsWith("/commit")) return kept as never;
    throw new Error(`unexpected ${path}`);
  });
  vi.mocked(api.post).mockImplementation(async (path: string) => {
    if (path.endsWith("/plans"))
      return {
        plans: [
          { id: "plan-1", name: "Test Plan", currency: "GBP", last_modified_on: null, first_month: null, last_month: null },
          { id: "plan-2", name: "Other Plan", currency: "EUR", last_modified_on: null, first_month: null, last_month: null },
        ],
      } as never;
    throw new Error(`unexpected ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HouseholdPage household={HOME} user={USER} onChanged={vi.fn()} />
    </QueryClientProvider>,
  );
}

function open(): HTMLElement {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Start a one-time import" }));
  return screen.getByRole("dialog", { name: "One-time Import" });
}

const nextButton = (panel: HTMLElement, name = "Next") =>
  within(panel).getByRole("button", { name }) as HTMLButtonElement;

const uploads = (suffix: string) =>
  vi.mocked(api.upload).mock.calls.filter(([path]) => String(path).endsWith(suffix));

function sentPlan(suffix: string): ImportPlan {
  const calls = uploads(suffix);
  const form = calls[calls.length - 1][1] as FormData;
  return JSON.parse(String(form.get("plan")));
}

/** Through source and connect with a file, to the review. */
async function toReview(): Promise<{ panel: HTMLElement; file: File }> {
  const panel = open();
  fireEvent.click(nextButton(panel));
  const file = new File(["Account,Flag,Date\n"], "Test Register.csv", { type: "text/csv" });
  fireEvent.change(panel.querySelector('input[type="file"]') as HTMLInputElement, {
    target: { files: [file] },
  });
  fireEvent.click(nextButton(panel));
  await within(panel).findByText("Cleared states");
  return { panel, file };
}

async function toAccounts() {
  const got = await toReview();
  await waitFor(() => expect(nextButton(got.panel).disabled).toBe(false));
  fireEvent.click(nextButton(got.panel));
  await within(got.panel).findByRole("combobox", { name: "Target for Alpha Current" });
  return got;
}

async function toOptions() {
  const got = await toAccounts();
  fireEvent.click(nextButton(got.panel)); // → categories
  await within(got.panel).findByRole("combobox", { name: "Target for Groceries" });
  fireEvent.click(nextButton(got.panel)); // → options
  await within(got.panel).findByText(/everything\s+arrives uncleared/);
  return got;
}

async function toPreview() {
  const got = await toOptions();
  fireEvent.click(within(got.panel).getByRole("checkbox", { name: /I understand YNAB's/ }));
  fireEvent.click(nextButton(got.panel, "Preview"));
  await within(got.panel).findByText(/Nothing has been imported yet/);
  return got;
}

describe("One-time Import section", () => {
  it("sits at the bottom of the household page and points at GitHub for more workflows", () => {
    const { container } = mount();
    const sections = container.querySelectorAll("section.card");
    const last = sections[sections.length - 1] as HTMLElement;
    expect(within(last).getByRole("heading", { name: "One-time Import" })).toBeTruthy();
    expect(last.textContent).toContain(
      "Other budgeting and spend tracker app import workflows can be added to this, just suggest it via GitHub",
    );
    const link = within(last).getByRole("link", { name: "GitHub" }) as HTMLAnchorElement;
    expect(link.href).toBe("https://github.com/MarioLonghi-com/household-spend-tracker/issues/new");
    expect(link.target).toBe("_blank");
    expect(link.rel).toContain("noopener");
  });
});

describe("The YNAB wizard", () => {
  it("says only the Register.csv is needed, and resends the file on every call", async () => {
    const panel = open();
    fireEvent.click(nextButton(panel));
    expect(panel.textContent).toContain(
      "You only need the *Register.csv file, not the *Plan.csv. The whole export .zip works too.",
    );
    expect((panel.querySelector('input[type="file"]') as HTMLInputElement).accept).toBe(".zip,.csv");
    // Nothing to send yet, so nothing to go on to.
    expect(nextButton(panel).disabled).toBe(true);
    panel.remove();
    cleanup();

    const { panel: again, file } = await toReview();
    const form = uploads("/analyse")[0][1] as FormData;
    expect(form.get("via")).toBe("csv");
    expect(form.get("file")).toBe(file);
    expect(form.get("token")).toBeNull();
    expect(uploads("/analyse")[0][0]).toBe("/households/hh-home/one-time-import/ynab/analyse");
    expect(again.textContent).toContain("20 reconciled");
  });

  it("marks the current step, ticks the finished ones and grows by the plan step for the API", async () => {
    const steps = (panel: HTMLElement) =>
      within(within(panel).getByRole("navigation", { name: "Steps" })).getAllByRole("listitem");
    const current = (panel: HTMLElement) =>
      steps(panel).filter((li) => li.getAttribute("aria-current") === "step");

    const panel = open();
    expect(steps(panel).map((li) => li.textContent)).toEqual([
      "1Step 1: Source app",
      "2Step 2: Connect",
      "3Step 3: Review",
      "4Step 4: Accounts",
      "5Step 5: Categories",
      "6Step 6: Flags & options",
      "7Step 7: Preview",
      "8Step 8: Report",
    ]);
    expect(current(panel).map((li) => li.textContent)).toEqual(["1Step 1: Source app"]);
    expect(steps(panel)[0].className).toContain("current");
    expect(steps(panel)[1].className).toContain("todo");

    fireEvent.click(nextButton(panel));
    expect(current(panel).map((li) => li.textContent)).toEqual(["2Step 2: Connect"]);
    expect(steps(panel)[0].className).toContain("done");
    expect(steps(panel)[0].textContent).toBe("✓Step 1, done: Source app");
    expect(panel.querySelector(".ynab-stepper-compact")?.textContent).toBe("Step 2 of 8 — Connect");

    // The API adds the plan step between Connect and Review.
    fireEvent.click(within(panel).getByRole("radio", { name: /API key/ }));
    expect(steps(panel).map((li) => li.textContent?.replace(/^\S+Step \d+(, done)?: /, ""))).toEqual([
      "Source app", "Connect", "Plan", "Review", "Accounts", "Categories", "Flags & options", "Preview", "Report",
    ]);
    expect(panel.querySelector(".ynab-stepper-compact")?.textContent).toBe("Step 2 of 9 — Connect");
    panel.remove();
    cleanup();

    const { panel: again } = await toAccounts();
    expect(current(again).map((li) => li.textContent)).toEqual(["4Step 4: Accounts"]);
    expect(steps(again).slice(0, 3).every((li) => li.className.includes("done"))).toBe(true);
    expect(steps(again).slice(4).every((li) => li.className.includes("todo"))).toBe(true);
  });

  it("tints each mapping row by the choice made on it, and says the state in words", async () => {
    const { panel } = await toAccounts();
    const row = (name: string) =>
      within(panel).getByRole("combobox", { name: `Target for ${name}` }).closest("tr") as HTMLElement;
    const pick = (name: string, value: string) =>
      fireEvent.change(within(panel).getByRole("combobox", { name: `Target for ${name}` }), {
        target: { value },
      });

    // As suggested: Alpha to an existing account, Beta to a new one.
    expect(row("Alpha Current").className).toBe("ynab-row-matched");
    expect(row("Alpha Current").textContent).toContain("✓ Matched");
    expect(row("Beta Card").className).toBe("ynab-row-create");
    expect(row("Beta Card").textContent).toContain("+ New");
    const legend = within(panel).getByRole("list", { name: "Row colours" });
    expect(legend.textContent).toContain("Skipped");

    // The tint follows the choice, not the suggestion.
    pick("Beta Card", "skip");
    expect(row("Beta Card").className).toBe("ynab-row-unmatched");
    expect(row("Beta Card").textContent).toContain("! Skipped");
    pick("Alpha Current", "create");
    expect(row("Alpha Current").className).toBe("ynab-row-create");
    pick("Beta Card", "existing:acc-2");
    expect(row("Beta Card").className).toBe("ynab-row-matched");

    fireEvent.click(nextButton(panel));
    await within(panel).findByRole("combobox", { name: "Target for Groceries" });
    expect(row("Groceries").className).toBe("ynab-row-matched");
    expect(row("Inflow: Ready to Assign").className).toBe("ynab-row-fixed");
    expect(row("Inflow: Ready to Assign").textContent).toContain("– Fixed");
    // The suggested marker stays beside the state word.
    expect(within(row("Groceries")).getByText("suggested")).toBeTruthy();

    pick("Groceries", "uncategorised");
    expect(row("Groceries").className).toBe("ynab-row-unmatched");
    expect(row("Groceries").textContent).toContain("! Uncategorised");
    pick("Groceries", "create");
    expect(row("Groceries").className).toBe("ynab-row-create");
    expect(row("Inflow: Ready to Assign").className).toBe("ynab-row-fixed");
  });

  it("promises the API key is not kept, and sends it only as a form field", async () => {
    const panel = open();
    fireEvent.click(nextButton(panel));
    fireEvent.click(within(panel).getByRole("radio", { name: /API key/ }));
    expect(panel.textContent).toContain(
      "Your key is used only for this import. It stays in this browser tab and is never stored or logged by Spend Tracker.",
    );
    const input = within(panel).getByLabelText("YNAB personal access token") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(
      (within(panel).getByRole("link", { name: /Developer settings/ }) as HTMLAnchorElement).href,
    ).toContain("app.ynab.com/settings/developer");

    fireEvent.change(input, { target: { value: "test-token-abc" } });
    fireEvent.click(nextButton(panel));
    await within(panel).findByRole("radio", { name: /Other Plan/ });
    expect(vi.mocked(api.post)).toHaveBeenCalledWith(
      "/households/hh-home/one-time-import/ynab/plans",
      { token: "test-token-abc" },
    );
    // A plan has to be confirmed before anything is read from it.
    expect(nextButton(panel, "Use this plan").disabled).toBe(true);
    fireEvent.click(within(panel).getByRole("radio", { name: /Test Plan/ }));
    fireEvent.click(nextButton(panel, "Use this plan"));
    await within(panel).findByText("Cleared states");
    const form = uploads("/analyse")[0][1] as FormData;
    expect(form.get("via")).toBe("api");
    expect(form.get("token")).toBe("test-token-abc");
    expect(form.get("plan_id")).toBe("plan-1");
    expect(form.get("file")).toBeNull();
  });

  it("warns when this household has had a YNAB import before", async () => {
    found = analysis({
      previous_imports: [
        { batch_id: "b-0", at: "2026-01-05T10:00:00", via: "csv", filename: "Old Register.csv", plan_name: null, status: "applied" },
      ],
    });
    const { panel } = await toReview();
    expect(within(panel).getByRole("alert").textContent).toContain(
      "has been imported into from YNAB before",
    );
  });

  it("lets an existing account take only one YNAB account", async () => {
    const { panel } = await toAccounts();
    const alpha = within(panel).getByRole("combobox", { name: "Target for Alpha Current" }) as HTMLSelectElement;
    const beta = within(panel).getByRole("combobox", { name: "Target for Beta Card" }) as HTMLSelectElement;
    const option = (select: HTMLSelectElement, value: string) =>
      select.querySelector(`option[value="${value}"]`) as HTMLOptionElement;

    // The suggestions are pre-selected and say so.
    expect(alpha.value).toBe("existing:acc-1");
    expect(beta.value).toBe("create");
    expect(option(alpha, "existing:acc-1").textContent).toContain("suggested");
    // Only accounts in the plan's currency are offered.
    expect(option(alpha, "existing:acc-3")).toBeNull();
    // The rich line: type, institution, masked id, balance, count, dates.
    expect(option(alpha, "existing:acc-1").textContent).toMatch(
      /Test Current · Current account · Example Bank · …1234 · .*500\.00 · 10 txns · 2024-06-01 – 2025-12-01/,
    );
    expect(option(beta, "existing:acc-2").textContent).toContain("(closed)");

    // Alpha holds acc-1, so Beta cannot have it.
    expect(option(beta, "existing:acc-1").disabled).toBe(true);
    expect(option(beta, "existing:acc-2").disabled).toBe(false);

    fireEvent.change(beta, { target: { value: "existing:acc-2" } });
    expect(option(alpha, "existing:acc-2").disabled).toBe(true);
    expect(option(beta, "existing:acc-1").disabled).toBe(true);

    // Letting go of one frees it for the other.
    fireEvent.change(alpha, { target: { value: "skip" } });
    expect(option(beta, "existing:acc-1").disabled).toBe(false);
  });

  it("keeps YNAB's own buckets uncategorised whatever else is chosen", async () => {
    const { panel } = await toAccounts();
    fireEvent.click(nextButton(panel));
    const fixed = (await within(panel).findByRole("combobox", {
      name: "Target for Inflow: Ready to Assign",
    })) as HTMLSelectElement;
    expect(fixed.disabled).toBe(true);
    expect(fixed.value).toBe("uncategorised");
    expect(panel.textContent).toContain("always arrive");

    const groceries = within(panel).getByRole("combobox", { name: "Target for Groceries" }) as HTMLSelectElement;
    expect(groceries.value).toBe("existing:cat-1");
    expect(groceries.disabled).toBe(false);
    // Many to one: the same target stays available to every row.
    fireEvent.change(groceries, { target: { value: "existing:cat-2" } });

    fireEvent.click(nextButton(panel));
    fireEvent.click(await within(panel).findByRole("checkbox", { name: /I understand YNAB's/ }));
    fireEvent.click(nextButton(panel, "Preview"));
    await waitFor(() => expect(uploads("/preview")).toHaveLength(1));
    const plan = sentPlan("/preview");
    expect(plan.categories).toEqual({
      Groceries: { kind: "existing", category_id: "cat-2" },
      "Inflow: Ready to Assign": { kind: "uncategorised" },
    });
    expect(plan.accounts).toEqual({
      "Alpha Current": { kind: "existing", account_id: "acc-1" },
      "Beta Card": { kind: "create", name: "Beta Card", type: "credit_card" },
    });
  });

  it("will not preview until the cleared-state reset is acknowledged", async () => {
    const { panel } = await toOptions();
    const preview = nextButton(panel, "Preview");
    expect(preview.disabled).toBe(true);
    fireEvent.click(preview);
    expect(uploads("/preview")).toHaveLength(0);

    const ack = within(panel).getByRole("checkbox", { name: /I understand YNAB's/ });
    fireEvent.click(ack);
    expect(nextButton(panel, "Preview").disabled).toBe(false);
    fireEvent.click(nextButton(panel, "Preview"));
    await waitFor(() => expect(uploads("/preview")).toHaveLength(1));
    const plan = sentPlan("/preview");
    expect(plan.acknowledge_cleared_reset).toBe(true);
    expect(plan.flags).toBe("memo");
    expect(plan.starting_balance).toBe("import");
    expect(plan.date_from).toBeNull();
    expect(plan.currency).toBe("GBP");
  });

  it("skips duplicates by default, one by one when ticked, and all of them on OK for all", async () => {
    dryRun = report({ duplicates: DUPLICATES });
    const { panel, file } = await toPreview();

    const boxes = () =>
      within(panel).getAllByRole("checkbox", { name: /Import anyway/ }) as HTMLInputElement[];
    expect(boxes().map((box) => box.checked)).toEqual([false, false]);
    expect(panel.textContent).toContain("0 of 2 will be imported");

    fireEvent.click(boxes()[1]);
    expect(panel.textContent).toContain("1 of 2 will be imported");

    fireEvent.click(within(panel).getByRole("checkbox", { name: /OK for all/ }));
    expect(boxes().every((box) => box.checked && box.disabled)).toBe(true);
    expect(panel.textContent).toContain("2 of 2 will be imported");

    fireEvent.click(nextButton(panel, "Import"));
    await waitFor(() => expect(uploads("/commit")).toHaveLength(1));
    expect(sentPlan("/commit").duplicates).toEqual({ all: "import", import: [] });
    const form = uploads("/commit")[0][1] as FormData;
    expect(form.get("file")).toBe(file);

    // And without it, only the ticked one goes up.
    cleanup();
    vi.mocked(api.upload).mockClear();
    const second = await toPreview();
    fireEvent.click(
      within(second.panel).getAllByRole("checkbox", { name: /Import anyway/ })[1],
    );
    fireEvent.click(nextButton(second.panel, "Import"));
    await waitFor(() => expect(uploads("/commit")).toHaveLength(1));
    const plan = sentPlan("/commit");
    expect(plan.duplicates.all).toBeNull();
    expect(plan.duplicates.import).toHaveLength(1);
    expect(["r-1", "r-2"]).toContain(plan.duplicates.import[0]);
  });

  it("reports what was created and downloads the not-imported rows as a dated .txt", async () => {
    kept = report({
      committed: true,
      batch_id: "b-1",
      created: {
        accounts: [{ id: "acc-9", name: "Beta Card", opening_date: "2025-01-31" }],
        categories: [{ id: "cat-9", name: "Hobbies" }],
        payees: 7,
      },
    });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    await within(panel).findByText(/fix its opening balance/);
    expect(panel.textContent).toContain(
      "New accounts were created with an opening balance of 0 dated the day before their first transaction — open each one and fix its opening balance.",
    );
    expect(panel.textContent).toContain("Beta Card");
    expect(panel.textContent).toContain("Hobbies");
    expect(panel.textContent).toContain("undone from");
    expect(panel.textContent).toContain("bad amount");

    // Its links open a new tab, so the report -- and the wizard -- stay put.
    for (const [name, screen] of [["Beta Card", "accounts"], ["History", "history"]]) {
      const link = within(panel).getByRole("link", { name });
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("href")).toMatch(new RegExp(`^/\\?open=${screen}&household=`));
    }
    expect(within(panel).getByRole("heading", { name: /Created/ })).toBeTruthy();
    // No bank text came, so no pointer at the rules it would feed (#265).
    expect(within(panel).queryByRole("link", { name: /Payee Naming Rules/ })).toBeNull();

    let saved: Blob | null = null;
    const create = vi.fn((blob: Blob) => {
      saved = blob;
      return "blob:test";
    });
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
    let name = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      name = this.download;
    });

    fireEvent.click(within(panel).getByRole("button", { name: "Download .txt" }));
    expect(create).toHaveBeenCalledTimes(1);
    expect(name).toMatch(/^ynab-import-not-imported-\d{4}-\d{2}-\d{2}\.txt$/);
    expect(await saved!.text()).toBe(kept.report_text);
  });

  it("counts the rule suggestions in the same note, behind the same one link (#269)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 1234, rule_suggestions: 12 });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const links = await within(panel).findAllByRole("link", { name: "Payee Naming Rules (opens in a new tab)" });
    expect(links).toHaveLength(1);
    expect(links[0].closest("[role=note]")!.textContent).toBe(
      `${(1234).toLocaleString()} transactions kept the bank’s own text. 12 groups of bank strings look like one payee each; make rules in Payee Naming Rules?`,
    );
    expect(links[0].getAttribute("target")).toBe("_blank");
    expect(links[0].getAttribute("href")).toMatch(/^\/\?open=rules&household=/);
    expect(links[0].getAttribute("rel")).toBe("noopener");
    expect(panel.textContent).not.toContain("after the first statement import");
    // Twelve fit on the Rules screen, so nothing says only some are listed.
    expect(panel.textContent).not.toContain("largest are listed");
  });

  it("says only the largest are listed when there are more than the Rules screen shows (#269)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 0, rule_suggestions: RULES_LISTED + 1 });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const link = await within(panel).findByRole("link", { name: "Payee Naming Rules (opens in a new tab)" });
    expect(link.closest("[role=note]")!.textContent).toBe(
      `21 groups of bank strings look like one payee each (the 20 largest are listed); make rules in Payee Naming Rules?`,
    );
  });

  it("does not say only some are listed at exactly the number the Rules screen shows (#269)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 0, rule_suggestions: RULES_LISTED });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const link = await within(panel).findByRole("link", { name: "Payee Naming Rules (opens in a new tab)" });
    expect(link.closest("[role=note]")!.textContent).toBe(
      "20 groups of bank strings look like one payee each; make rules in Payee Naming Rules?",
    );
  });

  it("counts suggestions that statements left, when this import brought no bank text (#269)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 0, rule_suggestions: 1 });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const link = await within(panel).findByRole("link", { name: "Payee Naming Rules (opens in a new tab)" });
    expect(link.closest("[role=note]")!.textContent).toBe(
      "1 group of bank strings looks like one payee; make a rule in Payee Naming Rules?",
    );
    expect(panel.textContent).not.toContain("after the first statement import");
  });

  it("says rules come after the first statement when no bank text came and none are suggested (#269)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 0, rule_suggestions: 0 });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const said = await within(panel).findByText(/after the first statement import/);
    expect(said.closest("[role=note]")!.textContent).toBe(
      "Rules can be suggested after the first statement import: this import brought no bank text to suggest them from.",
    );
    // Nothing to see there yet, so nothing points there.
    expect(within(panel).queryByRole("link", { name: /Payee Naming Rules/ })).toBeNull();
    expect(panel.textContent).not.toContain("look like one payee");
  });

  it("keeps the bank-text sentence alone when that text groups into no suggestion (#269)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 2, rule_suggestions: 0 });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const link = await within(panel).findByRole("link", { name: "Payee Naming Rules (opens in a new tab)" });
    expect(link.closest("[role=note]")!.textContent).toBe(
      "2 transactions kept the bank’s own text, so Payee Naming Rules may now suggest rules from it.",
    );
    expect(panel.textContent).not.toContain("look like one payee");
    expect(panel.textContent).not.toContain("after the first statement import");
  });

  it("points at the payee rules when the bank's own text came across (#265)", async () => {
    kept = report({ committed: true, batch_id: "b-1", bank_text_rows: 1234 });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const link = await within(panel).findByRole("link", { name: "Payee Naming Rules (opens in a new tab)" });
    expect(link.closest("[role=note]")!.textContent).toBe(
      `${(1234).toLocaleString()} transactions kept the bank’s own text, so Payee Naming Rules may now suggest rules from it.`,
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("href")).toMatch(/^\/\?open=rules&household=/);
  });

  it("names each account that does not add up to YNAB's balance, in its own currency (#266)", async () => {
    kept = report({
      committed: true,
      batch_id: "b-1",
      balance_differences: [
        {
          account_key: "acc-1",
          account: "Alpha Current",
          currency: "GBP",
          ynab_balance_minor: -6784,
          imported_minor: -6234,
          difference_minor: 550,
          sentence: "unused by the screen",
        },
        {
          account_key: "acc-2",
          account: "Yen Pot",
          currency: "JPY",
          ynab_balance_minor: 1234,
          imported_minor: 1000,
          difference_minor: -234,
          sentence: "unused by the screen",
        },
      ],
    });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const list = await waitFor(() => {
      const found = panel.querySelector(".ynab-balance-differences");
      expect(found).toBeTruthy();
      return found!;
    });
    const items = Array.from(list.querySelectorAll("li")).map((li) => li.textContent);
    expect(items).toEqual([
      `Alpha Current: YNAB’s balance is ${money(-6784, "GBP")}, and this import accounts for ${money(-6234, "GBP")}, ${money(550, "GBP")} more.`,
      `Yen Pot: YNAB’s balance is ${money(1234, "JPY")}, and this import accounts for ${money(1000, "JPY")}, ${money(234, "JPY")} less.`,
    ]);
    expect(money(1234, "JPY")).not.toContain("12.34");
    const note = list.closest("[role=note]")!.textContent;
    expect(note).toContain("These accounts do not add up");
    // The fixture's report has a failed row, so the note points at it.
    expect(note).toContain("The rows that failed, listed below, are where to look.");
  });

  it("does not point at failed rows when none failed, and keeps two accounts of one name apart (#266)", async () => {
    const twin = {
      currency: "GBP",
      ynab_balance_minor: 1000,
      imported_minor: 900,
      difference_minor: -100,
      sentence: "unused by the screen",
    };
    const base = report();
    kept = report({
      committed: true,
      batch_id: "b-1",
      counts: { ...base.counts, failed: 0 },
      balance_differences: [
        { ...twin, account_key: "acc-1", account: "Savings" },
        { ...twin, account_key: "acc-2", account: "Savings", imported_minor: 800, difference_minor: -200 },
      ],
      balance_unchecked: [
        {
          account_key: "acc-3",
          account: "Odd Pot",
          reason: "12345 thousandths is not a whole number of GBP minor units",
          sentence: "Odd Pot: YNAB's balance could not be checked: 12345 thousandths is not a whole number of GBP minor units.",
        },
      ],
    });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    const list = await waitFor(() => {
      const found = panel.querySelector(".ynab-balance-differences");
      expect(found).toBeTruthy();
      return found!;
    });
    expect(Array.from(list.querySelectorAll("li")).map((li) => li.textContent)).toEqual([
      `Savings: YNAB’s balance is ${money(1000, "GBP")}, and this import accounts for ${money(900, "GBP")}, ${money(100, "GBP")} less.`,
      `Savings: YNAB’s balance is ${money(1000, "GBP")}, and this import accounts for ${money(800, "GBP")}, ${money(200, "GBP")} less.`,
    ]);
    const note = list.closest("[role=note]")!.textContent;
    expect(note).not.toContain("listed below");
    expect(note).toContain("No row failed here, so compare the account in YNAB with what arrived.");
    expect(panel.querySelector(".ynab-balance-unchecked")!.textContent).toBe(
      "Odd Pot: YNAB's balance could not be checked: 12345 thousandths is not a whole number of GBP minor units.",
    );
  });

  it("says nothing about balances when every account adds up (#266)", async () => {
    kept = report({ committed: true, batch_id: "b-1", balance_differences: [] });
    const { panel } = await toPreview();
    fireEvent.click(nextButton(panel, "Import"));
    await within(panel).findByText(/in one batch/);
    expect(panel.querySelector(".ynab-balance-differences")).toBeNull();
    expect(panel.textContent).not.toContain("add up to YNAB");
  });

  it("takes a refused plan back to the step that holds the choice", async () => {
    const { panel } = await toOptions();
    const refusal = Object.assign(
      new Error("the new account for 'Beta Card': an account called 'Beta Card' already exists"),
      { status: 409 },
    );
    vi.mocked(api.upload).mockImplementation(async (path: string) => {
      if (path.endsWith("/preview")) throw refusal;
      return found as never;
    });
    fireEvent.click(within(panel).getByRole("checkbox", { name: /I understand YNAB's/ }));
    fireEvent.click(nextButton(panel, "Preview"));
    const alert = await within(panel).findByRole("alert");
    expect(alert.textContent).toContain("already exists");
    expect(within(panel).getByRole("combobox", { name: "Target for Beta Card" })).toBeTruthy();
    expect(within(panel).getByRole("heading", { name: "4. Accounts" })).toBeTruthy();
  });

  it("shows unpaired transfers, and the source's date where none could be read", async () => {
    const { panel } = await toPreview();
    expect(panel.textContent).toContain("1 transfer will be imported as ordinary transactions");
    expect(panel.textContent).toContain("the other side's account is skipped");
    expect(panel.textContent).toContain("2025-13-45");
  });
});
