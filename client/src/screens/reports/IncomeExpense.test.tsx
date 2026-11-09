// @vitest-environment jsdom

/**
 * What the report's table does with the rows it is handed.
 *
 * Every assertion here is about a *changed value on screen* -- a row that is
 * no longer rendered, a figure that carries the sign's colour, a group name
 * that appears once as a heading rather than twenty times beside the rows.
 * Asserting that a button exists, or that a click handler fired, would pass on
 * a fold that folded nothing, which is the whole of what this adds.
 *
 * `ReportTable` is rendered directly rather than through `IncomeExpense`, so
 * nothing here needs a server: the screen's job is to fetch the report, and
 * this is about what the report looks like once it has.
 */

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { CurrencyToggle, HeadSlot, ReportTable, drillLink, groupRows, toggleFold } from "./IncomeExpense";
import { registerPresetFrom } from "../../lib/registerLink";
import type { Household, IncomeExpense as Report, ReportLine, ReportRow } from "../../lib/types";
import { format } from "../../lib/money";

// `globals` is off in vite.config.ts, so Testing Library never finds an
// `afterEach` to hang its own cleanup on and every render would pile up in the
// same document -- which shows up as "found two Transport buttons" three tests
// later rather than as anything to do with the test that leaked.
afterEach(cleanup);

// Only the en-XA test opens a figure, which asks the server what went into it.
vi.mock("../../lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

const MONTHS = ["2026-08", "2026-09"];

/** Two of everything: two months, two groups, two categories in each. */
function row(
  name: string,
  group: string | null,
  values: [number, number],
  key: string | null = name.toLowerCase(),
): ReportRow {
  const total = values[0] + values[1];
  return {
    key,
    name,
    group_name: group,
    by_month: { "2026-08": values[0], "2026-09": values[1] },
    total_minor: total,
    total: String(total),
    average_minor: Math.trunc(total / 2),
    average: String(Math.trunc(total / 2)),
    count: 2,
  };
}

/** A line with no breakdown: a group's subtotal or a reimbursement line. */
function line(values: [number, number], name: string | null = null, count = 2): ReportLine {
  const total = values[0] + values[1];
  return {
    key: name ? `group-${name.toLowerCase()}` : null,
    name,
    by_month: { "2026-08": values[0], "2026-09": values[1] },
    total_minor: total,
    total: String(total),
    average_minor: Math.trunc(total / 2),
    average: String(Math.trunc(total / 2)),
    count,
  };
}

function section(rows: ReportRow[], byMonth: [number, number], groups: ReportLine[] = []) {
  const total = byMonth[0] + byMonth[1];
  return {
    rows,
    groups,
    by_month: { "2026-08": byMonth[0], "2026-09": byMonth[1] },
    total_minor: total,
    total: String(total),
    average_minor: Math.trunc(total / 2),
    average: String(Math.trunc(total / 2)),
    count: rows.length * 2,
  };
}

function aReport(over: Partial<Report> = {}): Report {
  return {
    currency: "EUR",
    since: "2026-08-01",
    until: "2026-09-30",
    months: MONTHS,
    income: section([row("Salary", "Earnings", [300000, 300000])], [300000, 300000]),
    expense: section(
      [
        row("Rent", "Housing", [-120000, -120000]),
        row("Power", "Housing", [-8000, -9000]),
        row("Fuel", "Transport", [-6000, -7000]),
        row("Tickets", "Transport", [-2000, -3000]),
        row("Uncategorised", null, [-1000, -400000], null),
      ],
      [-137000, -139000],
      [line([-128000, -129000], "Housing"), line([-8000, -10000], "Transport")],
    ),
    net_by_month: { "2026-08": 163000, "2026-09": -39000 },
    net_total_minor: 124000,
    net_total: "124000",
    reimbursements: {
      income: line([0, 35500]),
      spent: line([-30000, 0], null, 1),
      difference: line([0, 5500], null, 1),
      unmatched: line([0, 0], null, 0),
      pending: line([0, -4000], null, 1),
    },
    has_income_group: true,
    excluded: { transfers: 0, opening_balances: 0, reimbursements: 0 },
    coverage: { months_in_range: 2, months_with_activity: 2 },
    ...over,
  };
}

/** The screen's own fold state, driven through the screen's own transition. */
function Harness({ report = aReport(), query = "currency=EUR" }: { report?: Report; query?: string }) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  return (
    <ReportTable
      report={report}
      household={{ id: "household-1" } as Household}
      query={query}
      groupCategories={new Map([["group-housing", ["rent", "power"]]])}
      collapsed={collapsed}
      onToggle={(key) => setCollapsed((was) => toggleFold(was, key))}
    />
  );
}

/**
 * Every row label in the body, top to bottom, with the fold caret stripped.
 *
 * The order is the assertion in half of these: a group heading that rendered
 * but landed after its own categories would satisfy "it is on screen".
 */
const labels = () =>
  [...document.querySelectorAll("table.report tbody th.report-label")].map((cell) =>
    (cell.textContent ?? "").replace(/[▾▸]/g, "").trim(),
  );

describe("groupRows", () => {
  it("keeps the groups in the order their first row arrived in", () => {
    // Not alphabetical: the server sorts rows by size, so the group holding
    // the biggest figure has to stay at the top. Sorting here would move it.
    const grouped = groupRows([
      row("Rent", "Housing", [-1, -1]),
      row("Fuel", "Transport", [-1, -1]),
      row("Power", "Housing", [-1, -1]),
    ]);
    expect(grouped.map((one) => one.label)).toEqual(["Housing", "Transport"]);
    expect(grouped[0].rows.map((one) => one.name)).toEqual(["Rent", "Power"]);
  });

  it("puts the rows with no group last, and gives them no heading", () => {
    const grouped = groupRows([
      row("Uncategorised", null, [-1, -1], null),
      row("Rent", "Housing", [-1, -1]),
    ]);
    expect(grouped.map((one) => one.label)).toEqual(["Housing", null]);
    expect(grouped[1].rows.map((one) => one.name)).toEqual(["Uncategorised"]);
  });
});

describe("the report table", () => {
  it("prints a category group as one heading rather than beside every row", () => {
    render(<Harness />);

    // Once as a heading, and not again on Rent's own line.
    expect(screen.getAllByRole("button", { name: "Housing" })).toHaveLength(1);
    expect(labels()).toContain("Rent");
    expect(labels()).not.toContain("Rent · Housing");

    // And it sits *between* the groups: a heading, then the categories under
    // it, then the next heading -- with the ungrouped row last and unheaded.
    // Income is one group by construction, so it carries no group heading.
    expect(labels()).toEqual([
      "Income",
      "Salary",
      "Total income",
      "Expenses",
      "Housing",
      "Rent",
      "Power",
      "Transport",
      "Fuel",
      "Tickets",
      "Uncategorised",
      "Total expenses",
    ]);
  });

  it("puts the server's subtotal on each group heading, and keeps it when folded", () => {
    render(<Harness />);
    const housing = () =>
      screen.getByRole("button", { name: "Housing" }).closest("tr") as HTMLElement;
    const figures = () =>
      [...housing().querySelectorAll("td.amount")].map((cell) => cell.textContent?.trim());

    // Aug, Sep, Total, Average -- the group line from the response, not a sum
    // of Rent and Power made here (they add to -128,000 and -129,000 too, but
    // the average is the server's -128,500).
    expect(figures()).toEqual([-128000, -129000, -257000, -128500].map((n) => format(n, "EUR")));

    fireEvent.click(screen.getByRole("button", { name: "Housing" }));
    expect(labels()).not.toContain("Rent");
    expect(figures()).toEqual([-128000, -129000, -257000, -128500].map((n) => format(n, "EUR")));
  });

  it("shows the work reimbursements under Net, apart from it", () => {
    render(<Harness />);
    const foot = [...document.querySelectorAll("table.report tfoot th.report-label")].map(
      (cell) => cell.textContent?.trim(),
    );
    expect(foot).toEqual([
      "Net",
      "Work reimbursements",
      "Reimbursement income",
      "Amount spent, reimbursed",
      "Difference, counted above",
      "Not matched to an expense",
      "Pending reimbursement",
    ]);
    const difference = [...document.querySelectorAll("tr.report-aside")][2];
    expect(
      [...difference.querySelectorAll("td.amount")].map((cell) => cell.textContent?.trim()),
    ).toEqual([0, 5500, 5500, 2750].map((n) => format(n, "EUR")));
    const pending = [...document.querySelectorAll("tr.report-aside")][4];
    expect([...pending.querySelectorAll("td.amount")].map((cell) => cell.textContent?.trim())).toEqual(
      [0, -4000, -4000, -2000].map((n) => format(n, "EUR")),
    );
    expect(document.body.textContent).toContain("Only the difference is counted");
  });

  it("names a surplus row itself, never Uncategorised, and keeps it apart from the groups", () => {
    const surplus: ReportRow = {
      ...row("Reimbursement surplus", null, [0, 5500], null),
      line: "reimbursement_surplus",
    };
    const income = section(
      [row("Salary", "Earnings", [300000, 300000]), surplus],
      [300000, 305500],
    );
    render(<Harness report={aReport({ income })} />);
    const labels = [...document.querySelectorAll("table.report tbody th.report-label")].map(
      (cell) => cell.textContent?.trim(),
    );
    expect(labels).toContain("Reimbursement surplus");
    // The one Uncategorised row is the expense side's own; the surplus row is
    // keyless too, and must not be read as a second one.
    expect(labels.filter((label) => label === "Uncategorised")).toHaveLength(1);
    expect(document.querySelectorAll("tr.report-unset")).toHaveLength(1);
    const tr = [...document.querySelectorAll("tr.report-derived")];
    expect(tr).toHaveLength(1);
    expect([...tr[0].querySelectorAll("td.amount")].map((cell) => cell.textContent?.trim())).toEqual(
      [0, 5500, 5500, 2750].map((n) => format(n, "EUR")),
    );
  });

  it("puts the headline figures above the table, the reimbursement ones apart", () => {
    render(<Harness />);
    const figures = (selector: string) =>
      [...document.querySelectorAll(`${selector} > div`)].map((one) =>
        [one.querySelector("dt")?.textContent, one.querySelector("dd")?.textContent],
      );
    expect(figures(".report-headline > dl.report-figures")).toEqual([
      ["Income", format(600000, "EUR")],
      ["Expenses", format(-276000, "EUR")],
      ["Net", format(124000, "EUR")],
    ]);
    // Net is income plus expenses and nothing from the block below.
    expect(figures(".report-figures-aside > dl")).toEqual([
      ["Reimbursements received", format(35500, "EUR")],
      ["Amount spent, reimbursed", format(-30000, "EUR")],
      ["Still to be reimbursed", `${format(-4000, "EUR")} (1 item)`],
    ]);
  });

  it("says when no group is marked Income, and only then", () => {
    const { unmount } = render(<Harness />);
    expect(document.querySelector(".report-no-income")).toBeNull();
    unmount();
    render(<Harness report={aReport({ has_income_group: false })} />);
    expect(document.querySelector(".report-no-income")?.textContent).toContain(
      "No category group is marked as Income",
    );
  });

  it("folds a band away and keeps its total on screen", () => {
    render(<Harness />);
    expect(labels()).toContain("Rent");

    fireEvent.click(screen.getByRole("button", { name: "Expenses" }));

    // The categories and their group headings are gone...
    expect(labels()).not.toContain("Rent");
    expect(labels()).not.toContain("Housing");
    expect(screen.queryByRole("button", { name: "Housing" })).toBeNull();
    // ...the income band is untouched...
    expect(labels()).toContain("Salary");
    // ...and the figure the band exists to give is still there.
    expect(labels()).toEqual([
      "Income",
      "Salary",
      "Total income",
      "Expenses",
      "Total expenses",
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Expenses" }));
    expect(labels()).toContain("Rent");
  });

  it("folds one category group without touching its neighbour", () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole("button", { name: "Housing" }));

    expect(labels()).not.toContain("Rent");
    expect(labels()).not.toContain("Power");
    // The heading stays, so there is something to unfold...
    expect(screen.getByRole("button", { name: "Housing" }).getAttribute("aria-expanded")).toBe(
      "false",
    );
    // ...the other group is still open...
    expect(labels()).toContain("Fuel");
    // ...and so is the row that belongs to no group.
    expect(labels()).toContain("Uncategorised");
  });

  it("rolls a folded group up into its months, and tells only a screen reader the count", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Transport" }));

    const heading = screen.getByRole("button", { name: "Transport" }).closest("tr")!;
    expect([...heading.querySelectorAll("td.amount")].map((cell) => cell.textContent?.trim())).toEqual(
      [-8000, -10000, -18000, -9000].map((n) => format(n, "EUR")),
    );
    const said = heading.querySelector(".sr-only");
    expect(said?.textContent).toBe("2 categories folded away");
    expect(heading.querySelector(".report-folded")).toBeNull();
  });

  it("colours the net line by its sign and keeps the minus on a negative", () => {
    render(<Harness />);
    const net = document.querySelector("tr.report-net") as HTMLElement;

    const up = net.querySelectorAll(".net-up");
    const down = net.querySelectorAll(".net-down");
    // August's net and the whole-period net are positive; September's is not.
    expect(up).toHaveLength(2);
    expect(down).toHaveLength(1);

    // The sign, not the colour, is what a monochrome print carries.
    expect(down[0].textContent).toContain("-");
    expect(up[0].textContent).not.toContain("-");
    // And the word a screen reader hears, since it is told no colour at all.
    expect(down[0].textContent).toContain("shortfall");
    expect(up[0].textContent).toContain("surplus");
  });

  it("gives the two summary columns their own heading cells at the end", () => {
    render(<Harness />);
    const head = document.querySelector("table.report thead tr") as HTMLElement;
    const cells = [...head.querySelectorAll("th")].map((one) => one.textContent?.trim());
    // The inverted banding is keyed off these two being the last cells in the
    // row -- `td.amount:last-child` and `:nth-last-child(2)` -- so their
    // position is load-bearing rather than cosmetic.
    expect(cells.slice(-2)).toEqual(["Total", "Average"]);
    expect(cells.slice(1, -2)).toEqual(["Aug 2026", "Sep 2026"]);
  });
});

describe("what the report says it left out", () => {
  const notes = () => document.querySelector(".report-notes")?.textContent ?? "";

  it("counts the rows set apart beside the transfers", () => {
    render(
      <Harness
        report={aReport({ excluded: { transfers: 2, opening_balances: 0, reimbursements: 3 } })}
      />,
    );
    expect(notes()).toContain("Not counted: 2 transfer legs and 0 opening balances.");
    expect(notes()).toContain("3 rows set apart under Work reimbursements");
  });

  it("says it even when nothing else was left out, and in the singular for one", () => {
    render(
      <Harness
        report={aReport({ excluded: { transfers: 0, opening_balances: 0, reimbursements: 1 } })}
      />,
    );
    expect(notes()).toContain("1 row set apart under Work reimbursements");
    expect(notes()).not.toContain("Not counted");
  });

  it("stays quiet at zero", () => {
    render(<Harness />);
    expect(notes()).not.toContain("set apart");
    expect(notes()).not.toContain("another currency");
  });

  it("says how many repayments cover expenses in another currency", () => {
    render(<Harness report={aReport({ reimbursement_mixed: 2 })} />);
    expect(notes()).toContain(
      "2 repayments cover expenses in another currency, so no surplus or shortfall is worked out for them.",
    );
  });
});

describe("the heading slot", () => {
  it("puts the toggle inside the shell's slot, on the heading's line", () => {
    // The shell, as `Reports.tsx` renders it: the slot first, the heading
    // second, both in one row.
    const shell = document.createElement("div");
    shell.innerHTML = '<div class="row"><div id="report-head-slot"></div><h1>Income vs Expense</h1></div>';
    document.body.append(shell);

    render(
      <HeadSlot>
        <span data-testid="toggle">EUR</span>
      </HeadSlot>,
    );

    const slot = document.getElementById("report-head-slot") as HTMLElement;
    expect(slot.textContent).toBe("EUR");
    // Asserting the text alone would pass on a toggle that rendered in its own
    // fallback line below: what changed is *which parent* it hangs off.
    expect(screen.getByTestId("toggle").closest("#report-head-slot")).toBe(slot);
    expect(document.querySelector(".report-head")).toBeNull();
    // And the heading is still beside it rather than displaced by a wrapper.
    expect(slot.nextElementSibling?.tagName).toBe("H1");

    shell.remove();
  });

  it("keeps the toggle on screen when there is no slot", () => {
    // The report rendered outside that shell. Blanking the toggle here would
    // leave a page of figures with nothing saying what currency they are in.
    render(
      <HeadSlot>
        <span data-testid="toggle">EUR</span>
      </HeadSlot>,
    );

    const fallback = document.querySelector(".report-head") as HTMLElement;
    expect(fallback).not.toBeNull();
    expect(screen.getByTestId("toggle").closest(".report-head")).toBe(fallback);
  });
});

describe("in en-XA, the report shows no English", () => {
  it("the table, its notes, a folded band and group, a figure's rows and the currency hint", async () => {
    const { activate } = await import("../../lib/i18n");
    const { untranslated } = await import("../../test-pseudo");
    const { api } = await import("../../lib/api");
    await activate("en-XA");
    try {
      const report = aReport({
        excluded: { transfers: 3, opening_balances: 1, reimbursements: 2 },
        coverage: { months_in_range: 2, months_with_activity: 1 },
      });
      vi.mocked(api.get).mockResolvedValue({
        entries: [{ id: "t1", account_name: "Visa", date: "2026-08-02", payee_name: "Landlord", memo: null, amount_minor: -120000 }],
        count: 40,
        total_minor: -240000,
        capped: true,
      });
      // The fixture's own names are data.
      const data = new Set(
        [...report.income.rows, ...report.expense.rows].flatMap((one) => [one.name, one.group_name ?? ""]),
      );
      const left = () => untranslated(document.body).filter((word) => !data.has(word) && !/^(Visa|Landlord)$/.test(word));
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <Harness report={report} />
          <CurrencyToggle value="EUR" options={["EUR", "GBP"]} onChange={() => undefined} />
        </QueryClientProvider>,
      );
      expect(left()).toEqual([]);

      // Fold a group in the expense band, then the income band.
      const folds = Array.from(document.querySelectorAll<HTMLButtonElement>("button.fold"));
      fireEvent.click(folds.find((one) => one.textContent?.includes("Housing"))!);
      expect(left()).toEqual([]);
      fireEvent.click(folds[0]);
      expect(left()).toEqual([]);

      // What went into a figure.
      fireEvent.click(document.querySelector<HTMLButtonElement>("button.figure-open")!);
      await screen.findByText("Landlord");
      expect(left()).toEqual([]);

      // The hint beside the currency toggle.
      fireEvent.click(document.querySelector<HTMLButtonElement>(".currency-toggle .hint-open")!);
      expect(left()).toEqual([]);
    } finally {
      await activate("en");
    }
  });
});


describe("a figure's rows open in Transactions (#401)", () => {
  const context = {
    household: "household-1",
    query: "currency=EUR&account_id=acc-1",
    since: "2026-08-01",
    until: "2026-09-30",
    currency: "EUR",
    groupCategories: new Map([["group-housing", ["rent", "power"]]]),
  };
  const read = (href: string) => registerPresetFrom(href.slice(1));

  it("filters a category's month to that category, that month, the currency and the accounts", () => {
    const preset = read(drillLink({ title: "", period: "2026-08", rowCategoryId: "rent", direction: "out" }, { id: "t1", date: "2026-08-02" }, context));
    expect(preset).toEqual({
      open: "t1",
      range: { since: "2026-08-01", until: "2026-08-31" },
      categories: ["rent"],
      backlog: false,
      currencies: ["EUR"],
      accounts: ["acc-1"],
    });
  });

  it("opens a group's figure on the group's categories, and Uncategorised on Needs a category", () => {
    const group = read(drillLink({ title: "", rowGroupId: "group-housing", direction: "out" }, { id: "t1", date: "2026-09-02" }, context));
    expect(group?.categories).toEqual(["rent", "power"]);
    expect(group?.range).toEqual({ since: "2026-08-01", until: "2026-09-30" });
    const unset = read(drillLink({ title: "", period: "2026-09", rowUncategorised: true, direction: "out" }, { id: "t2", date: "2026-09-12" }, context));
    expect(unset?.categories).toEqual([]);
    expect(unset?.backlog).toBe(true);
  });

  it("keeps the report's own category filter for a section total", () => {
    const preset = read(
      drillLink({ title: "", period: "2026-08", direction: "out" }, { id: "t1", date: "2026-08-02" }, {
        ...context,
        query: "currency=EUR&category_id=rent&category_id=fuel&include_uncategorised=false",
      }),
    );
    expect(preset?.categories).toEqual(["rent", "fuel"]);
    expect(preset?.backlog).toBe(false);
  });

  it("sets no category on a reimbursement line, and widens to a claim's earlier expense", () => {
    const preset = read(
      drillLink({ title: "", period: "2026-09", direction: "reimbursement_surplus" }, { id: "hotel", date: "2026-06-14" }, context),
    );
    expect(preset?.categories).toBeNull();
    expect(preset?.range).toEqual({ since: "2026-06-01", until: "2026-09-30" });
  });

  it("puts the link on each row of the bubble, which is twice as wide as a dialog", async () => {
    const { api } = await import("../../lib/api");
    vi.mocked(api.get).mockResolvedValue({
      entries: [{ id: "t-rent", account_name: "Visa", date: "2026-08-02", payee_name: "Landlord", category_name: "Rent", memo: null, amount_minor: -120000 }],
      count: 1,
      total_minor: -120000,
      capped: false,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Harness query="currency=EUR&account_id=acc-1" />
      </QueryClientProvider>,
    );
    const rent = [...document.querySelectorAll("table.report tbody tr")].find(
      (tr) => tr.querySelector("th")?.textContent === "Rent",
    )!;
    fireEvent.click(rent.querySelector<HTMLButtonElement>("button.figure-open")!);
    await screen.findByText("Landlord");

    expect(document.querySelector(".dialog")?.classList.contains("dialog-wide")).toBe(true);
    const link = document.querySelector<HTMLAnchorElement>("table.behind a.behind-open")!;
    expect(link.target).toBe("_blank");
    expect(read(link.getAttribute("href")!)).toEqual({
      open: "t-rent",
      range: { since: "2026-08-01", until: "2026-08-31" },
      categories: ["rent"],
      backlog: false,
      currencies: ["EUR"],
      accounts: ["acc-1"],
    });
    // The category is a column of its own now there is room for it.
    expect(screen.getByRole("columnheader", { name: "Category" })).toBeTruthy();

    const open = vi.spyOn(window, "open").mockReturnValue(null);
    fireEvent.click(screen.getByText("Landlord"));
    expect(open).toHaveBeenCalledWith(link.getAttribute("href"), "_blank", "noopener");
    open.mockRestore();
  });
});
