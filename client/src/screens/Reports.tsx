/**
 * Reports: an index of what can be asked, and whichever report is open.
 *
 * The index is the section's own page rather than a tab bar on top of one
 * report. "Reports" is a place a person looks for things, and the thing they
 * are looking for is the list -- a screen that opens straight into whichever
 * report happens to be first tells them nothing about what else exists.
 *
 * Which report is open is the shell's state, not this file's: the nav lists
 * the reports as children of the Reports header, so opening one from there and
 * opening one from the index have to land in the same place. `REPORTS` is the
 * one list both read.
 *
 * Everything in this section is **read-only**. A report is a question, not an
 * act: it stores nothing, changes nothing, and appears nowhere in History,
 * because there is nothing to undo.
 */

import type { ReactNode } from "react";
import { IncomeExpense } from "./reports/IncomeExpense";
import { Reimbursements } from "./reports/Reimbursements";
import type { Household } from "../lib/types";
import type { RegisterPreset } from "./Register";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

export type ReportKey = "income-expense" | "reimbursements";

/**
 * What a report may ask of the shell, which owns navigation.
 *
 * One thing so far: open the register already narrowed, which is how the
 * Reimbursements report sends somebody from a figure to the rows behind it.
 * A report is still read-only -- it goes somewhere else to act.
 */
export type ReportNav = {
  openRegister: (preset: RegisterPreset) => void;
};

/** What the shell calls the screen showing one report. */
export type ReportScreen = `report-${ReportKey}`;

export function reportScreen(key: ReportKey): ReportScreen {
  return `report-${key}`;
}

export const REPORTS: {
  key: ReportKey;
  /** As the nav and the index both say it. One spelling, in one place. */
  label: string;
  blurb: string;
  render: (household: Household, nav: ReportNav) => ReactNode;
}[] = [
  {
    key: "income-expense",
    get label() {
      return t`Income vs Expense`;
    },
    get blurb() {
      return t`What came in, what went out, and what it was for — month by month.`;
    },
    render: (household) => <IncomeExpense household={household} />,
  },
  {
    key: "reimbursements",
    get label() {
      return t`Reimbursements`;
    },
    get blurb() {
      return t`What work owes you, what came back, and what was written off.`;
    },
    render: (household, nav) => (
      <Reimbursements household={household} onOpenRegister={nav.openRegister} />
    ),
  },
];

export function reportFor(screen: string): (typeof REPORTS)[number] | undefined {
  return REPORTS.find((one) => reportScreen(one.key) === screen);
}

/** The index. Every report there is, and what each one answers. */
export function Reports({
  household,
  onOpen,
}: {
  household: Household;
  onOpen: (key: ReportKey) => void;
}) {
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <h1><Trans>Reports</Trans></h1>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          Questions about {household.name}'s ledger. Nothing here changes anything: a report
          reads the register and is gone, which is why none of them appear in History.
        </Trans>
      </p>

      <ul className="report-index">
        {REPORTS.map((one) => (
          <li key={one.key}>
            <button type="button" className="report-card" onClick={() => onOpen(one.key)}>
              <span className="report-card-name">{one.label}</span>
              <span className="muted small">{one.blurb}</span>
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}

/**
 * One open report, with the way back to the index above it.
 *
 * The heading is the report's own name and the crumb is a real button: a
 * person who arrived here from the nav has no back button to press, and the
 * browser's does not apply to a shell that never changed URL.
 */
export function Report({
  household,
  screen,
  onBack,
  onOpenRegister,
}: {
  household: Household;
  screen: ReportScreen;
  onBack: () => void;
  onOpenRegister: ReportNav["openRegister"];
}) {
  const report = reportFor(screen);
  if (!report) {
    // Unreachable while the nav and the index both build from REPORTS, and
    // still not a blank screen if one of them ever stops.
    return (
      <>
        <h1><Trans>Reports</Trans></h1>
        <p className="muted small"><Trans>That report does not exist.</Trans></p>
        <button onClick={onBack}><Trans>Back to the reports</Trans></button>
      </>
    );
  }

  return (
    <>
      <nav className="crumbs" aria-label={t`Where you are`}>
        <button type="button" className="link" onClick={onBack}>
          <Trans>
            Reports
          </Trans>
        </button>
        <span aria-hidden="true">›</span>
        <span>{report.label}</span>
      </nav>
      <div
        className="row"
        style={{ justifyContent: "space-between", gap: 16, alignItems: "baseline", marginBottom: 4 }}
      >
        <h1>{report.label}</h1>
        {/* A slot a report can portal its own controls into, so something that
            belongs beside the heading can sit there without this file knowing
            what any report contains. Income v Expense puts its currency toggle
            here.

            It sits after the `<h1>`, at the right end of the heading line: the
            heading says what you are looking at and belongs where reading
            starts, and the control that changes it sits opposite, which is
            where every other control on this screen already is.

            Empty is the normal state — a report that portals nothing leaves it
            empty, and `#report-head-slot:empty` in `styles/shell.css` takes it
            out of the flex row so `space-between` has nothing to push against
            and the heading does not drift. */}
        <div id="report-head-slot" />
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        {report.blurb}
      </p>

      {report.render(household, { openRegister: onOpenRegister })}
    </>
  );
}
