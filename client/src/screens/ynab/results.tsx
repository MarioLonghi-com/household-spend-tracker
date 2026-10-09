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
import { formatDate } from "../../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { formatCount } from "../../lib/locale";
import { codedText } from "../../lib/noticeMessages";

export function Counts({ report }: { report: ImportReport }) {
  const it = report.counts;
  const done = report.committed;
  // The third field marks the one count that is bad news when it is not zero.
  const lines: [string, number, boolean?][] = [
    [t`Rows in the source`, it.rows_in_file],
    [done ? t({ message: "Imported", comment: "Label on the one-time import's results: brought in by an import" }) : t`Will be imported`, it.imported],
    [t({ message: "Transfers linked", comment: "Label on the one-time import's results" }), it.transfers_linked],
    [t({ message: "Duplicates skipped", comment: "Label on the one-time import's results" }), it.duplicates_skipped],
    [t`Duplicates imported anyway`, it.duplicates_imported],
    [t`Skipped: account set to Skip`, it.skipped_account],
    [t`Skipped: outside the date range`, it.skipped_date_range],
    [t`Skipped: YNAB Starting Balance`, it.skipped_starting_balance],
    [t({ message: "Failed", comment: "Label on the one-time import's results: it did not work" }), it.failed, true],
  ];
  return (
    <dl className="facts ynab-counts">
      {lines.map(([label, value, bad]) => (
        <div key={label} style={{ display: "contents" }}>
          <dt>{label}</dt>
          <dd className={bad && value > 0 ? "neg" : undefined}>
            {formatCount(value)}
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
        {plural(duplicates.length, {
          one: `${formatCount(duplicates.length)} possible duplicate`,
          other: `${formatCount(duplicates.length)} possible duplicates`,
        })}
      </h3>
      <p className="small">
        <Trans>
          These look like transactions already in the ledger: same account, same amount, dates
          within a few days. They are skipped unless you tick them. Either way the report lists
          them.
        </Trans>
      </p>
      <label className="check">
        <input
          type="checkbox"
          checked={importAll}
          onChange={(event) => onImportAll(event.target.checked)}
        />
        <Trans>
          OK for all — import every one of them anyway
        </Trans>
      </label>
      <p className="small muted">
        {t`${formatCount(importing)} of ${formatCount(duplicates.length)} will be imported.`}
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th><Trans comment="Column heading on the one-time import's results">Import anyway</Trans></th>
              <SortHeading label={t({ message: "Date", comment: "Column heading on the one-time import's results: noun. See GLOSSARY.md" })} column="date" {...heading} />
              <SortHeading label={t({ message: "Account", comment: "Column heading on the one-time import's results: noun, a bank or cash account. See GLOSSARY.md" })} column="account" {...heading} />
              <SortHeading label={t({ message: "Payee", comment: "Column heading on the one-time import's results: noun, who was paid or who paid. See GLOSSARY.md" })} column="payee" {...heading} />
              <SortHeading label={t({ message: "Amount", comment: "Column heading on the one-time import's results: noun, a sum of money. See GLOSSARY.md" })} column="amount" align="right" {...heading} />
              <th><Trans>Already in the ledger</Trans></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.row_ref}>
                <td data-select="true">
                  <input
                    type="checkbox"
                    aria-label={t({ message: `Import anyway: ${formatDate(row.date)} ${row.payee ?? ""} ${money(row.amount_minor, currency)}`, comment: "Screen-reader name on the one-time import's results" })}
                    checked={importAll || chosen.has(row.row_ref)}
                    disabled={importAll}
                    onChange={(event) => onToggle(row.row_ref, event.target.checked)}
                  />
                </td>
                <td className="small mono" data-label={t({ message: "Date", comment: "Column name shown beside a value on phones on the one-time import's results: noun. See GLOSSARY.md" })} data-detail-first="true">
                  {formatDate(row.date)}
                </td>
                <td className="small" data-label={t({ message: "Account", comment: "Column name shown beside a value on phones on the one-time import's results: noun, a bank or cash account. See GLOSSA…" })}>
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
                <td className="small muted" data-label={t({ message: "Existing", comment: "Column name shown beside a value on phones on the one-time import's results: adjective, already in the ledger" })}>
                  {formatDate(row.existing.date)} · {row.existing.payee ?? "—"} ·{" "}
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
            return codedText(row.reason, row.reason_code, row.reason_params);
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
            <SortHeading label={t({ message: "Date", comment: "Column heading on the one-time import's results: noun. See GLOSSARY.md" })} column="date" {...heading} />
            <SortHeading label={t({ message: "Account", comment: "Column heading on the one-time import's results: noun, a bank or cash account. See GLOSSARY.md" })} column="account" {...heading} />
            <SortHeading label={t({ message: "Payee", comment: "Column heading on the one-time import's results: noun, who was paid or who paid. See GLOSSARY.md" })} column="payee" {...heading} />
            <SortHeading label={t({ message: "Amount", comment: "Column heading on the one-time import's results: noun, a sum of money. See GLOSSARY.md" })} column="amount" align="right" {...heading} />
            <SortHeading label={t({ message: "Why", comment: "Column heading on the one-time import's results: noun, the reason" })} column="reason" {...heading} />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.row_ref}>
              <td className="small mono" data-label={t({ message: "Date", comment: "Column name shown beside a value on phones on the one-time import's results: noun. See GLOSSARY.md" })} data-detail-first="true">
                {row.date ?? row.date_text ?? "—"}
              </td>
              <td className="small" data-label={t({ message: "Account", comment: "Column name shown beside a value on phones on the one-time import's results: noun, a bank or cash account. See GLOSSA…" })}>
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
              <td className="small" data-label={t({ message: "Why", comment: "Column name shown beside a value on phones on the one-time import's results: noun, the reason" })}>
                {codedText(row.reason, row.reason_code, row.reason_params)}
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
        {committed
          ? plural(rows.length, {
              one: `${formatCount(rows.length)} transfer imported as ordinary transactions, with the reason for each`,
              other: `${formatCount(rows.length)} transfers imported as ordinary transactions, with the reason for each`,
            })
          : plural(rows.length, {
              one: `${formatCount(rows.length)} transfer will be imported as ordinary transactions, with the reason for each`,
              other: `${formatCount(rows.length)} transfers will be imported as ordinary transactions, with the reason for each`,
            })}
      </summary>
      <p className="small muted">
        <Trans>
          The other side of each was not imported, so there is nothing here to link it to.
        </Trans>
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
    <a href={href} target="_blank" rel="noopener" aria-label={t`Payee Naming Rules (opens in a new tab)`}>
      <Trans>
        Payee Naming Rules
      </Trans>
    </a>
  );
  // The Rules screen lists the largest few and never says "20 of N".
  const cap = groups > RULES_LISTED ? ` ${t`(the ${RULES_LISTED} largest are listed)`}` : "";
  if (kept === 0 && groups === 0) {
    // Null is a preview, or a server from before the count: say nothing.
    if (report.rule_suggestions !== 0) return null;
    return (
      <div className="banner info" role="note" style={{ marginTop: 12 }}>
        <Trans>
          Rules can be suggested after the first statement import: this import brought no bank
          text to suggest them from.
        </Trans>
      </div>
    );
  }
  const keptText = plural(kept, {
    one: `${formatCount(kept)} transaction kept the bank’s own text`,
    other: `${formatCount(kept)} transactions kept the bank’s own text`,
  });
  if (groups === 0) {
    return (
      <div className="banner info" role="note" style={{ marginTop: 12 }}>
        <Trans>
          {keptText}, so {rules} may now suggest rules from it.
        </Trans>
      </div>
    );
  }
  const groupsText = formatCount(groups);
  return (
    <div className="banner info" role="note" style={{ marginTop: 12 }}>
      {kept > 0 ? `${keptText}. ` : ""}
      {groups === 1 ? (
        <Trans>1 group of bank strings looks like one payee; make a rule in {rules}?</Trans>
      ) : (
        <Trans>
          {groupsText} groups of bank strings look like one payee each{cap}; make rules in {rules}?
        </Trans>
      )}
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
  const imported = plural(report.counts.imported, {
    one: `Imported ${formatCount(report.counts.imported)} transaction in one batch.`,
    other: `Imported ${formatCount(report.counts.imported)} transactions in one batch.`,
  });
  return (
    <>
      <div className="banner info" role="status">
        <Trans>
          {imported} The whole import — transactions, accounts, categories and payees — can be
          undone from{" "}
          <a href={hrefFor("history")} target="_blank" rel="noopener">
            History
          </a>{" "}
          as one entry.
        </Trans>
      </div>

      <Counts report={report} />

      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Heading on the one-time import's results">
          Created
        </Trans>
      </h3>
      <dl className="facts">
        <dt><Trans comment="Name of a fact on the one-time import's results: noun, bank or cash accounts. See GLOSSARY.md">Accounts</Trans></dt>
        <dd>
          {created.accounts.length === 0
            ? t({ message: "None", context: "created", comment: "Value of a fact on the one-time import's results (created)" })
            : created.accounts.map((one) => one.name).join(", ")}
        </dd>
        <dt><Trans comment="Name of a fact on the one-time import's results. See GLOSSARY.md">Categories</Trans></dt>
        <dd>
          {created.categories.length === 0
            ? t({ message: "None", context: "created", comment: "Value of a fact on the one-time import's results (created)" })
            : created.categories.map((one) => one.name).join(", ")}
        </dd>
        <dt><Trans comment="Name of a fact on the one-time import's results: noun, who was paid or who paid. See GLOSSARY.md">Payees</Trans></dt>
        <dd>{formatCount(created.payees)}</dd>
      </dl>

      {created.accounts.length > 0 ? (
        <div className="banner warn" role="note" style={{ marginTop: 12 }}>
          <Trans>
            New accounts were created with an opening balance of 0 dated the day before their
            first transaction — open each one and fix its opening balance.
          </Trans>
          <ul className="plain-list" style={{ marginTop: 6 }}>
            {created.accounts.map((one) => (
              <li key={one.id}>
                <a href={hrefFor("accounts")} target="_blank" rel="noopener">
                  {one.name}
                </a>{" "}
                <span className="small muted">{t({ message: `opened ${formatDate(one.opening_date)}`, comment: "List item on the one-time import's results" })}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(report.balance_differences ?? []).length > 0 ? (
        <div className="banner warn" role="note" style={{ marginTop: 12 }}>
          {plural(report.balance_differences!.length, {
            one: "This account does not add up to YNAB’s balance.",
            other: "These accounts do not add up to YNAB’s balance.",
          })}{" "}
          {report.counts.failed > 0
            ? t`The rows that failed, listed below, are where to look.`
            : t`No row failed here, so compare the account in YNAB with what arrived.`}
          <ul className="plain-list ynab-balance-differences" style={{ marginTop: 6 }}>
            {report.balance_differences!.map((one, at) => (
              <li key={one.account_key ?? at}>
                {one.difference_minor > 0
                  ? t`${one.account}: YNAB’s balance is ${money(one.ynab_balance_minor, one.currency)}, and this import accounts for ${money(one.imported_minor, one.currency)}, ${money(Math.abs(one.difference_minor), one.currency)} more.`
                  : t`${one.account}: YNAB’s balance is ${money(one.ynab_balance_minor, one.currency)}, and this import accounts for ${money(one.imported_minor, one.currency)}, ${money(Math.abs(one.difference_minor), one.currency)} less.`}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(report.balance_unchecked ?? []).length > 0 ? (
        <div className="banner warn" role="note" style={{ marginTop: 12 }}>
          <ul className="plain-list ynab-balance-unchecked">
            {report.balance_unchecked!.map((one, at) => (
              <li key={one.account_key ?? at}>{codedText(one.sentence, one.sentence_code, one.sentence_params)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <RulesNote report={report} href={hrefFor("rules")} />

      <details className="ynab-not-imported" open={report.not_imported.length > 0 && report.not_imported.length <= 20}>
        <summary>
          {t`${formatCount(report.not_imported.length)} not imported, with the reason for each`}
        </summary>
        {report.not_imported.length > 0 ? (
          <NotImportedTable rows={report.not_imported} currency={currency} />
        ) : (
          <p className="small muted"><Trans>Every row was imported.</Trans></p>
        )}
      </details>
      <UnpairedTransfers rows={report.unpaired_transfers ?? []} currency={currency} committed />
      <p>
        <button type="button" onClick={() => downloadReport(report.report_text)}>
          <Trans comment="Button on the one-time import's results">
            Download .txt
          </Trans>
        </button>{" "}
        <span className="small muted">
          <Trans>Every row that was not imported, and why.</Trans>
        </span>
      </p>
    </>
  );
}
