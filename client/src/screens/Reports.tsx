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
import { IncomeExpense, latestTwelve, type IncomeExpensePreset } from "./reports/IncomeExpense";
import { Reimbursements } from "./reports/Reimbursements";
import { NetWorth } from "./reports/NetWorth";
import type { Household } from "../lib/types";
import type { RegisterPreset } from "./Register";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { HelpMark } from "../help/HelpMark";

export type ReportKey = "income-expense" | "reimbursements" | "net-worth";

/**
 * What a report may ask of the shell, which owns navigation.
 *
 * Open the register already narrowed, which is how the Reimbursements report
 * sends somebody from a figure to the rows behind it; and open the Exchange
 * rates page, where a converting report sends somebody whose rates are
 * missing or old (#396, #397). A report is still read-only -- it goes
 * somewhere else to act.
 */
export type ReportNav = {
  openRegister: (preset: RegisterPreset) => void;
  openExchangeRates?: () => void;
};

/**
 * Settings a report opens with, from a preset card on the index. Only
 * Income vs Expense has any; its own type says what they are.
 */
export type ReportPreset = IncomeExpensePreset;

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
  render: (household: Household, nav: ReportNav, preset?: ReportPreset) => ReactNode;
}[] = [
  {
    key: "income-expense",
    get label() {
      return t`Income vs Expense`;
    },
    get blurb() {
      return t`What came in, what went out, and what it was for — month by month.`;
    },
    render: (household, nav, preset) => (
      <IncomeExpense
        household={household}
        preset={preset}
        onOpenExchangeRates={nav.openExchangeRates}
      />
    ),
  },
  {
    key: "reimbursements",
    get label() {
      return t({ message: "Reimbursements", comment: "Label on the Reports screen. See GLOSSARY.md" });
    },
    get blurb() {
      return t`What work owes you, what came back, and what was written off.`;
    },
    render: (household, nav) => (
      <Reimbursements household={household} onOpenRegister={nav.openRegister} />
    ),
  },
  {
    key: "net-worth",
    get label() {
      return t({ message: "Net worth", comment: "Label on the Reports screen: everything the household has, less what it owes" });
    },
    get blurb() {
      return t`How much the household has, all accounts together, at each month end — and what moved it.`;
    },
    render: (household, nav) => (
      <NetWorth
        household={household}
        onOpenRegister={nav.openRegister}
        onOpenExchangeRates={nav.openExchangeRates}
      />
    ),
  },
];

export function reportFor(screen: string): (typeof REPORTS)[number] | undefined {
  return REPORTS.find((one) => reportScreen(one.key) === screen);
}

/**
 * Ready-made views of a report: a card on the index, not a report of their own.
 *
 * A preset opens the report it names with settings chosen for one question,
 * and from there it *is* that report -- every control works as ever. "Latest
 * 12 months" (#396 §8) is Income vs Expense over the twelve closed months,
 * every currency in the base: the year as it stands, with no month
 * half-counted. `settings` is a function so the months are today's when the
 * card is pressed, not when the page was loaded.
 */
export const PRESETS: {
  key: string;
  report: ReportKey;
  label: string;
  blurb: string;
  settings: () => ReportPreset;
}[] = [
  {
    key: "latest-12-months",
    report: "income-expense",
    get label() {
      return t({ message: "Latest 12 months", comment: "A card on the Reports screen: Income vs Expense over the twelve months before this one" });
    },
    get blurb() {
      return t`Income vs Expense over the last twelve whole months, every currency in your base currency.`;
    },
    settings: () => latestTwelve(),
  },
];

/** The index. Every report there is, and what each one answers. */
export function Reports({
  household,
  onOpen,
}: {
  household: Household;
  /** Open a report; a preset card passes the settings it opens with. */
  onOpen: (key: ReportKey, preset?: ReportPreset) => void;
}) {
  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <div className="screen-title"><h1><Trans comment="Screen title on the Reports screen. See GLOSSARY.md">Reports</Trans></h1><HelpMark /></div>
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
        {PRESETS.map((one) => (
          <li key={one.key}>
            <button
              type="button"
              className="report-card"
              onClick={() => onOpen(one.report, one.settings())}
            >
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
  preset,
  onBack,
  onOpenRegister,
  onOpenExchangeRates,
}: {
  household: Household;
  screen: ReportScreen;
  /** The settings a preset card opened this report with, if one did. */
  preset?: ReportPreset;
  onBack: () => void;
  onOpenRegister: ReportNav["openRegister"];
  onOpenExchangeRates?: ReportNav["openExchangeRates"];
}) {
  const report = reportFor(screen);
  if (!report) {
    // Unreachable while the nav and the index both build from REPORTS, and
    // still not a blank screen if one of them ever stops.
    return (
      <>
        <h1><Trans comment="Screen title on the Reports screen. See GLOSSARY.md">Reports</Trans></h1>
        <p className="muted small"><Trans>That report does not exist.</Trans></p>
        <button onClick={onBack}><Trans>Back to the reports</Trans></button>
      </>
    );
  }

  return (
    <>
      <nav className="crumbs" aria-label={t`Where you are`}>
        <button type="button" className="link" onClick={onBack}>
          <Trans comment="Button on the Reports screen. See GLOSSARY.md">
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
        <div className="screen-title"><h1>{report.label}</h1><HelpMark /></div>
        {/* A slot a report can portal its own controls into, so something that
            belongs beside the heading can sit there without this file knowing
            what any report contains. Income vs Expense puts its currency toggle
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

      {report.render(
        household,
        { openRegister: onOpenRegister, openExchangeRates: onOpenExchangeRates },
        preset,
      )}
    </>
  );
}
