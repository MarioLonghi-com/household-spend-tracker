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
import { CurrencyToggle, HeadSlot, ReportTable, groupRows, toggleFold } from "./IncomeExpense";
import type { Household, IncomeExpense as Report, ReportRow } from "../../lib/types";

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

function section(rows: ReportRow[], byMonth: [number, number]) {
  const total = byMonth[0] + byMonth[1];
  return {
    rows,
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
    ),
    net_by_month: { "2026-08": 163000, "2026-09": -39000 },
    net_total_minor: 124000,
    net_total: "124000",
    excluded: { transfers: 0, opening_balances: 0, reimbursements: 0 },
    coverage: { months_in_range: 2, months_with_activity: 2 },
    ...over,
  };
}

/** The screen's own fold state, driven through the screen's own transition. */
function Harness({ report = aReport() }: { report?: Report }) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  return (
    <ReportTable
      report={report}
      household={{ id: "household-1" } as Household}
      query="currency=EUR"
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
    expect(labels()).toEqual([
      "Income",
      "Earnings",
      "Salary",
      "Total income",
      "Expense",
      "Housing",
      "Rent",
      "Power",
      "Transport",
      "Fuel",
      "Tickets",
      "Uncategorised",
      "Total expense",
    ]);
  });

  it("folds a band away and keeps its total on screen", () => {
    render(<Harness />);
    expect(labels()).toContain("Rent");

    fireEvent.click(screen.getByRole("button", { name: "Expense" }));

    // The categories and their group headings are gone...
    expect(labels()).not.toContain("Rent");
    expect(labels()).not.toContain("Housing");
    expect(screen.queryByRole("button", { name: "Housing" })).toBeNull();
    // ...the income band is untouched...
    expect(labels()).toContain("Salary");
    // ...and the figure the band exists to give is still there.
    expect(labels()).toEqual([
      "Income",
      "Earnings",
      "Salary",
      "Total income",
      "Expense",
      "Total expense",
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Expense" }));
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

  it("says how many categories a fold took away", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Transport" }));
    expect(document.body.textContent).toContain("2 categories folded away");
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
    expect(cells.slice(-2)).toEqual(["Average", "Total"]);
    expect(cells.slice(1, -2)).toEqual(["Aug 2026", "Sep 2026"]);
  });
});

describe("what the report says it left out", () => {
  const notes = () => document.querySelector(".report-notes")?.textContent ?? "";

  it("counts the work expenses and repayments beside the transfers", () => {
    render(
      <Harness
        report={aReport({ excluded: { transfers: 2, opening_balances: 0, reimbursements: 3 } })}
      />,
    );
    expect(notes()).toContain("Not counted: 2 transfer legs and 0 opening balances.");
    expect(notes()).toContain("3 work expenses and repayments left out");
  });

  it("says it even when nothing else was left out, and in the singular for one", () => {
    render(
      <Harness
        report={aReport({ excluded: { transfers: 0, opening_balances: 0, reimbursements: 1 } })}
      />,
    );
    expect(notes()).toContain("1 work expense or repayment left out");
    expect(notes()).not.toContain("Not counted");
  });

  it("stays quiet at zero", () => {
    render(<Harness />);
    expect(notes()).not.toContain("work expense");
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

      // Fold the expense band and a group in the income band.
      const folds = Array.from(document.querySelectorAll<HTMLButtonElement>("button.fold"));
      fireEvent.click(folds[1]);
      fireEvent.click(folds.find((one) => one.textContent?.includes("Earnings"))!);
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

