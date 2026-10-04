/**
 * Steps 8 and 9 of the YNAB wizard: the dry run's verdict, and what was kept.
 *
 * Both are an `ImportReport` from the server. The preview is the same import
 * rolled back, so its counts are the real ones rather than a guess made here.
 */

import { useMemo } from "react";
import { SortHeading, sortRows, useSort } from "../../components/bits";
import { money } from "./mapping";
import type { Duplicate, ImportReport, NotImported } from "./types";

export function Counts({ report }: { report: ImportReport }) {
  const it = report.counts;
  const done = report.committed;
  const lines: [string, number][] = [
    ["Rows in the source", it.rows_in_file],
    [done ? "Imported" : "Will be imported", it.imported],
    ["Transfers linked", it.transfers_linked],
    ["Duplicates skipped", it.duplicates_skipped],
    ["Duplicates imported anyway", it.duplicates_imported],
    ["Skipped: account set to Skip", it.skipped_account],
    ["Skipped: outside the date range", it.skipped_date_range],
    ["Skipped: YNAB Starting Balance", it.skipped_starting_balance],
    ["Failed", it.failed],
  ];
  return (
    <dl className="facts ynab-counts">
      {lines.map(([label, value]) => (
        <div key={label} style={{ display: "contents" }}>
          <dt>{label}</dt>
          <dd className={label === "Failed" && value > 0 ? "neg" : undefined}>
            {value.toLocaleString()}
          </dd>
        </div>
      ))}
    </dl>
  );
}

type DuplicateSort = "date" | "account" | "payee" | "amount";

export function Duplicates({
  duplicates,
  currency,
  importAll,
  chosen,
  onImportAll,
  onToggle,
}: {
  duplicates: Duplicate[];
  currency: string;
  importAll: boolean;
  chosen: Set<string>;
  onImportAll: (all: boolean) => void;
  onToggle: (rowRef: string, importIt: boolean) => void;
}) {
  const { sort, direction, onSort } = useSort<DuplicateSort>("date");
  const rows = useMemo(
    () =>
      sortRows(duplicates, sort, direction, (row, column) => {
        switch (column) {
          case "date":
            return row.date;
          case "account":
            return row.account;
          case "payee":
            return row.payee;
          case "amount":
            return row.amount_minor;
        }
      }),
    [duplicates, sort, direction],
  );
  const heading = { sort, direction, onSort };
  const importing = importAll ? duplicates.length : duplicates.filter((d) => chosen.has(d.row_ref)).length;

  return (
    <>
      <h3 className="section-title">
        {duplicates.length.toLocaleString()} possible{" "}
        {duplicates.length === 1 ? "duplicate" : "duplicates"}
      </h3>
      <p className="small">
        These look like transactions already in the ledger: same account, same amount, dates
        within a few days. They are skipped unless you tick them. Either way the report lists
        them.
      </p>
      <label className="check">
        <input
          type="checkbox"
          checked={importAll}
          onChange={(event) => onImportAll(event.target.checked)}
        />
        OK for all — import every one of them anyway
      </label>
      <p className="small muted">
        {importing.toLocaleString()} of {duplicates.length.toLocaleString()} will be imported.
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Import anyway</th>
              <SortHeading label="Date" column="date" {...heading} />
              <SortHeading label="Account" column="account" {...heading} />
              <SortHeading label="Payee" column="payee" {...heading} />
              <SortHeading label="Amount" column="amount" align="right" {...heading} />
              <th>Already in the ledger</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.row_ref}>
                <td data-select="true">
                  <input
                    type="checkbox"
                    aria-label={`Import anyway: ${row.date} ${row.payee ?? ""} ${money(row.amount_minor, currency)}`}
                    checked={importAll || chosen.has(row.row_ref)}
                    disabled={importAll}
                    onChange={(event) => onToggle(row.row_ref, event.target.checked)}
                  />
                </td>
                <td className="small mono" data-label="Date" data-detail-first="true">
                  {row.date}
                </td>
                <td className="small" data-label="Account">
                  {row.account}
                </td>
                <td data-primary="true">
                  {row.payee ?? <span className="muted">—</span>}
                  {row.memo ? <div className="small muted">{row.memo}</div> : null}
                </td>
                <td className="amount" data-figure="true">
                  <span className={row.amount_minor < 0 ? "neg" : undefined}>
                    {money(row.amount_minor, currency)}
                  </span>
                </td>
                <td className="small muted" data-label="Existing">
                  {row.existing.date} · {row.existing.payee ?? "—"} ·{" "}
                  {money(row.existing.amount_minor, currency)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

type NotImportedSort = "date" | "account" | "payee" | "amount" | "reason";

export function NotImportedTable({ rows, currency }: { rows: NotImported[]; currency: string }) {
  const { sort, direction, onSort } = useSort<NotImportedSort>("date");
  const sorted = useMemo(
    () =>
      sortRows(rows, sort, direction, (row, column) => {
        switch (column) {
          case "date":
            return row.date ?? row.date_text;
          case "account":
            return row.account;
          case "payee":
            return row.payee;
          case "amount":
            return row.amount_minor;
          case "reason":
            return row.reason;
        }
      }),
    [rows, sort, direction],
  );
  const heading = { sort, direction, onSort };
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <SortHeading label="Date" column="date" {...heading} />
            <SortHeading label="Account" column="account" {...heading} />
            <SortHeading label="Payee" column="payee" {...heading} />
            <SortHeading label="Amount" column="amount" align="right" {...heading} />
            <SortHeading label="Why" column="reason" {...heading} />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.row_ref}>
              <td className="small mono" data-label="Date" data-detail-first="true">
                {row.date ?? row.date_text ?? "—"}
              </td>
              <td className="small" data-label="Account">
                {row.account ?? "—"}
              </td>
              <td data-primary="true">
                {row.payee ?? <span className="muted">—</span>}
                {row.memo ? <div className="small muted">{row.memo}</div> : null}
              </td>
              <td className="amount" data-figure="true">
                {row.amount_minor === null ? (
                  <span className="muted">—</span>
                ) : (
                  <span className={row.amount_minor < 0 ? "neg" : undefined}>
                    {money(row.amount_minor, currency)}
                  </span>
                )}
              </td>
              <td className="small" data-label="Why">
                {row.reason}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Transfers that arrived as ordinary transactions, because the other side was
 * skipped, out of range, a duplicate or failed. Imported, so not in the
 * not-imported list, but worth a look: each is half of a transfer.
 */
export function UnpairedTransfers({
  rows,
  currency,
  committed,
}: {
  rows: NotImported[];
  currency: string;
  committed: boolean;
}) {
  if (rows.length === 0) return null;
  return (
    <details className="ynab-not-imported">
      <summary>
        {rows.length.toLocaleString()} {rows.length === 1 ? "transfer" : "transfers"}{" "}
        {committed ? "imported" : "will be imported"} as ordinary transactions, with the reason
        for each
      </summary>
      <p className="small muted">
        The other side of each was not imported, so there is nothing here to link it to.
      </p>
      <NotImportedTable rows={rows} currency={currency} />
    </details>
  );
}

/** `ynab-import-not-imported-2026-09-30.txt`, dated by the browser's own day. */
export function reportFilename(today: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const day = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
  return `ynab-import-not-imported-${day}.txt`;
}

export function downloadReport(text: string): void {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = reportFilename();
  document.body.appendChild(link);
  link.click();
  link.remove();
  // After the click has had its turn: revoking at once can cancel the save.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * How many suggestions the Rules screen lists: `payees.SUGGESTIONS_LISTED` on
 * the server, and `test_client_agrees` fails if the two part.
 */
export const RULES_LISTED = 20;

/**
 * The one note about Payee Naming Rules (#265, #269): what bank text came, how
 * many groups of bank strings now look like one payee each, and -- after an
 * import that brought no bank text and left nothing to suggest -- that rules
 * wait for the first statement. One link, whichever it says.
 */
function RulesNote({ report, href }: { report: ImportReport; href: string }) {
  const kept = report.bank_text_rows;
  const groups = report.rule_suggestions ?? 0;
  const rules = (
    <a href={href} target="_blank" rel="noopener" aria-label="Payee Naming Rules (opens in a new tab)">
      Payee Naming Rules
    </a>
  );
  // The Rules screen lists the largest few and never says "20 of N".
  const cap = groups > RULES_LISTED ? ` (the ${RULES_LISTED} largest are listed)` : "";
  if (kept === 0 && groups === 0) {
    // Null is a preview, or a server from before the count: say nothing.
    if (report.rule_suggestions !== 0) return null;
    return (
      <div className="banner info" role="note" style={{ marginTop: 12 }}>
        Rules can be suggested after the first statement import: this import brought no bank
        text to suggest them from.
      </div>
    );
  }
  const keptText = `${kept.toLocaleString()} ${kept === 1 ? "transaction" : "transactions"} kept the bank’s own text`;
  if (groups === 0) {
    return (
      <div className="banner info" role="note" style={{ marginTop: 12 }}>
        {keptText}, so {rules} may now suggest rules from it.
      </div>
    );
  }
  return (
    <div className="banner info" role="note" style={{ marginTop: 12 }}>
      {kept > 0 ? `${keptText}. ` : ""}
      {groups === 1
        ? "1 group of bank strings looks like one payee; make a rule in "
        : `${groups.toLocaleString()} groups of bank strings look like one payee each${cap}; make rules in `}
      {rules}?
    </div>
  );
}

export function Report({
  report,
  currency,
  householdId,
}: {
  report: ImportReport;
  currency: string;
  /** Links open in a new tab, so the report stays where it is. */
  householdId?: string;
}) {
  const hrefFor = (screen: "accounts" | "history" | "rules") =>
    `/?open=${screen}${householdId ? `&household=${encodeURIComponent(householdId)}` : ""}`;
  const created = report.created;
  return (
    <>
      <div className="banner info" role="status">
        Imported {report.counts.imported.toLocaleString()}{" "}
        {report.counts.imported === 1 ? "transaction" : "transactions"} in one batch. The whole
        import — transactions, accounts, categories and payees — can be undone from{" "}
        <a href={hrefFor("history")} target="_blank" rel="noopener">
          History
        </a>{" "}
        as one entry.
      </div>

      <Counts report={report} />

      <h3 className="section-title" style={{ marginTop: 18 }}>
        Created
      </h3>
      <dl className="facts">
        <dt>Accounts</dt>
        <dd>
          {created.accounts.length === 0
            ? "None"
            : created.accounts.map((one) => one.name).join(", ")}
        </dd>
        <dt>Categories</dt>
        <dd>
          {created.categories.length === 0
            ? "None"
            : created.categories.map((one) => one.name).join(", ")}
        </dd>
        <dt>Payees</dt>
        <dd>{created.payees.toLocaleString()}</dd>
      </dl>

      {created.accounts.length > 0 ? (
        <div className="banner warn" role="note" style={{ marginTop: 12 }}>
          New accounts were created with an opening balance of 0 dated the day before their first
          transaction — open each one and fix its opening balance.
          <ul className="plain-list" style={{ marginTop: 6 }}>
            {created.accounts.map((one) => (
              <li key={one.id}>
                <a href={hrefFor("accounts")} target="_blank" rel="noopener">
                  {one.name}
                </a>{" "}
                <span className="small muted">opened {one.opening_date}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(report.balance_differences ?? []).length > 0 ? (
        <div className="banner warn" role="note" style={{ marginTop: 12 }}>
          {report.balance_differences!.length === 1 ? "This account does" : "These accounts do"} not
          add up to YNAB&rsquo;s balance.{" "}
          {report.counts.failed > 0
            ? "The rows that failed, listed below, are where to look."
            : "No row failed here, so compare the account in YNAB with what arrived."}
          <ul className="plain-list ynab-balance-differences" style={{ marginTop: 6 }}>
            {report.balance_differences!.map((one, at) => (
              <li key={one.account_key ?? at}>
                {one.account}: YNAB&rsquo;s balance is {money(one.ynab_balance_minor, one.currency)}, and
                this import accounts for {money(one.imported_minor, one.currency)},{" "}
                {money(Math.abs(one.difference_minor), one.currency)}{" "}
                {one.difference_minor > 0 ? "more" : "less"}.
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(report.balance_unchecked ?? []).length > 0 ? (
        <div className="banner warn" role="note" style={{ marginTop: 12 }}>
          <ul className="plain-list ynab-balance-unchecked">
            {report.balance_unchecked!.map((one, at) => (
              <li key={one.account_key ?? at}>{one.sentence}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <RulesNote report={report} href={hrefFor("rules")} />

      <details className="ynab-not-imported" open={report.not_imported.length > 0 && report.not_imported.length <= 20}>
        <summary>
          {report.not_imported.length.toLocaleString()} not imported, with the reason for each
        </summary>
        {report.not_imported.length > 0 ? (
          <NotImportedTable rows={report.not_imported} currency={currency} />
        ) : (
          <p className="small muted">Every row was imported.</p>
        )}
      </details>
      <UnpairedTransfers rows={report.unpaired_transfers ?? []} currency={currency} committed />
      <p>
        <button type="button" onClick={() => downloadReport(report.report_text)}>
          Download .txt
        </button>{" "}
        <span className="small muted">Every row that was not imported, and why.</span>
      </p>
    </>
  );
}
