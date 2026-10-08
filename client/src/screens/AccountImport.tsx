/**
 * Accounts from a file, many at once and all or none (#146).
 *
 * Two visits to the server with the same file. The first is a dry run: the
 * server runs every row through the code the one-at-a-time form uses and
 * throws the result away, so the preview is the real verdict rather than a
 * copy of its rules. Confirm sends the same file again to be kept. Nothing is
 * decided here about whether a row is importable -- a row is when the server
 * sent no problems with it.
 */

import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { format, toInput } from "../lib/money";
import {
  Field,
  moneyKey,
  Panel,
  Problem,
  SortHeading,
  sortRows,
  useSort,
} from "../components/bits";
import { countryName, TYPES, TypeHelp, typeLabel, useCountries } from "./Accounts";
import type { AccountImportOut, AccountImportRow, AccountType, Household } from "../lib/types";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { formatDate } from "../lib/locale";

type ImportSort =
  | "line"
  | "name"
  | "type"
  | "currency"
  | "country"
  | "opening_balance"
  | "opening_date"
  | "iban"
  | "problems";

/** The type's label when the server read one, otherwise what the file said. */
function rowType(row: AccountImportRow): string {
  const known = TYPES.some((one) => one.value === row.type);
  return known ? typeLabel(row.type as AccountType) : row.type;
}

/**
 * An amount in the row's currency.
 *
 * The currency is whatever the file said, and a row whose currency is not a
 * code at all still has a figure to show beside its problem -- `Intl` throws
 * on a malformed code, so that one is written out plainly instead.
 */
function amount(minor: number, currency: string): string {
  try {
    return format(minor, currency);
  } catch {
    return `${minor < 0 ? "-" : ""}${toInput(minor, currency)} ${currency}`;
  }
}

function send(household: Household, file: File, dryRun: boolean) {
  const form = new FormData();
  form.append("file", file);
  form.append("dry_run", dryRun ? "true" : "false");
  return api.upload<AccountImportOut>(`/households/${household.id}/accounts/import`, form);
}

export function AccountImport({
  household,
  onClose,
  onImported,
}: {
  household: Household;
  onClose: () => void;
  onImported: (result: AccountImportOut) => void;
}) {
  const countries = useCountries();
  const [file, setFile] = useState<File | null>(null);
  const { sort, direction, onSort } = useSort<ImportSort>("line");

  const preview = useMutation({ mutationFn: (chosen: File) => send(household, chosen, true) });
  const commit = useMutation({
    mutationFn: (chosen: File) => send(household, chosen, false),
    onSuccess: onImported,
  });

  function choose(chosen: File | null) {
    // A file chosen while the last one is being kept would reset the commit
    // and replace the preview under it, and the import that then finishes
    // is not the one on screen. The input is disabled for this; this is the
    // same rule for anything that reaches it anyway.
    if (commit.isPending) return;
    setFile(chosen);
    commit.reset();
    if (chosen) preview.mutate(chosen);
    else preview.reset();
  }

  const read = preview.data;
  const refused = read ? read.rows.filter((row) => row.problems.length > 0).length : 0;
  const count = read?.rows.length ?? 0;

  const rows = useMemo(
    () =>
      sortRows(
        read?.rows ?? [],
        sort,
        direction,
        (row, column) => {
          switch (column) {
            case "line":
              return row.line;
            case "name":
              return row.name;
            case "type":
              return rowType(row);
            case "currency":
              return row.currency;
            // By name, not by flag or code: the code files Spain under E.
            case "country":
              return row.country ? countryName(countries.data, row.country) : null;
            // Currency first, then the figure: minor units of two currencies
            // are not comparable quantities.
            case "opening_balance":
              return row.opening_balance === null
                ? null
                : moneyKey(row.currency, row.opening_balance, household.base_currency);
            case "opening_date":
              return row.opening_date;
            case "iban":
              return row.iban;
            case "problems":
              return row.problems.join(" ");
          }
        },
        (a, b) => a.line - b.line,
      ),
    [read, sort, direction, countries.data, household.base_currency],
  );
  const heading = { sort, direction, onSort };

  return (
    <Panel title={t`Import accounts from a file`} onClose={onClose} config wide>
      <p className="small">
        <Trans>
          One row per account, under the template's header row.{" "}
          <a href={`/api/households/${household.id}/accounts/import-template.csv`} download>
            Download the template
          </a>
          , fill it in with a spreadsheet, and save it as CSV.
        </Trans>
      </p>
      <dl className="facts">
        {/* The template's column names, which are the same in every language. */}
        <dt>name</dt>
        <dd><Trans>Required. No two accounts can share one.</Trans></dd>
        <dt>
          type <TypeHelp />
        </dt>
        <dd>
          {t`Required, one of`}{" "}
          {TYPES.map((one, at) => (
            <span key={one.value}>
              {at > 0 ? ", " : null}
              <span className="mono">{one.value}</span> ({one.label})
            </span>
          ))}
          .
        </dd>
        <dt>currency</dt>
        <dd>
          <Trans>A three-letter code. Leave it blank for {household.base_currency}.</Trans>
        </dd>
        <dt>country</dt>
        <dd>
          <Trans>
            Its two-letter code, like <span className="mono">ES</span>.
          </Trans>
        </dd>
        <dt>opening_balance</dt>
        <dd>
          <Trans>
            A plain figure like <span className="mono">1234.56</span> or{" "}
            <span className="mono">-80.00</span> — a point for the decimals and no thousands
            separator. Blank for an account that starts empty.
          </Trans>
        </dd>
        <dt>opening_date</dt>
        <dd>
          <Trans>
            <span className="mono">YYYY-MM-DD</span>, like <span className="mono">2026-01-31</span>.
            Blank for today.
          </Trans>
        </dd>
      </dl>
      <p className="small muted">
        <Trans>
          Every column but name and type can be left blank. Institution, iban and note are kept as
          written.
        </Trans>
      </p>
      <Field label={t({ message: "Accounts file", comment: "Label of a form field on the accounts import" })}>
        <input
          type="file"
          accept=".csv,text/csv"
          disabled={commit.isPending}
          onChange={(e) => {
            choose(e.target.files?.[0] ?? null);
            // So choosing the same file again, once it has been corrected,
            // reads it again rather than doing nothing.
            e.target.value = "";
          }}
        />
      </Field>
      {file && <p className="small muted">{file.name}</p>}

      <Problem error={preview.error ?? commit.error} />
      {preview.isPending && (
        <p className="small muted">
          <Trans>Reading the file…</Trans>
        </p>
      )}

      {read && (
        <>
          <p className="small">
            {refused === 0
              ? plural(count, {
                  one: `${count} account ready to import. Nothing has been created yet.`,
                  other: `${count} accounts ready to import. Nothing has been created yet.`,
                })
              : plural(count, {
                  one: `${refused} of ${count} row has a problem, so nothing can be imported yet. Correct the file and choose it again.`,
                  other: `${refused} of ${count} rows have a problem, so nothing can be imported yet. Correct the file and choose it again.`,
                })}
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading label={t({ message: "Line", comment: "Column heading on the accounts import: noun, a line of a file" })} column="line" align="right" {...heading} />
                  <SortHeading label={t({ message: "Account", comment: "Column heading on the accounts import: noun, a bank or cash account. See GLOSSARY.md" })} column="name" {...heading} />
                  <SortHeading label={t({ message: "Type", comment: "Column heading on the accounts import: noun, account type" })} column="type" {...heading} />
                  <SortHeading label={t({ message: "Currency", comment: "Column heading on the accounts import: noun. See GLOSSARY.md" })} column="currency" {...heading} />
                  <SortHeading
                    label={t({ message: "Country", comment: "Column heading on the accounts import: noun. See GLOSSARY.md" })}
                    column="country"
                    className="flag-col"
                    {...heading}
                  />
                  <SortHeading
                    label={t({ message: "Opening balance", comment: "Column heading on the accounts import: noun, what the account held on the day it starts. See GLOSSARY.md" })}
                    column="opening_balance"
                    align="right"
                    {...heading}
                  />
                  <SortHeading label={t({ message: "Opening date", comment: "Column heading on the accounts import" })} column="opening_date" {...heading} />
                  <SortHeading label={t({ message: "IBAN", comment: "Column heading on the accounts import: International Bank Account Number; keep as is. See GLOSSARY.md" })} column="iban" {...heading} />
                  <SortHeading label={t({ message: "Problems", comment: "Column heading on the accounts import" })} column="problems" {...heading} />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.line}>
                    <td className="amount muted" data-label={t({ message: "Line", comment: "Column name shown beside a value on phones on the accounts import: noun, a line of a file" })} data-detail-first="true">
                      {row.line}
                    </td>
                    <td data-primary="true">{row.name || <span className="muted">—</span>}</td>
                    <td className="small muted" data-label={t({ message: "Type", comment: "Column name shown beside a value on phones on the accounts import: noun, account type" })}>
                      {rowType(row) || "—"}
                    </td>
                    <td className="small muted mono" data-label={t({ message: "Currency", comment: "Column name shown beside a value on phones on the accounts import: noun. See GLOSSARY.md" })}>
                      {row.currency}
                    </td>
                    <td
                      className="flag-col"
                      data-label={t({ message: "Country", comment: "Column name shown beside a value on phones on the accounts import: noun. See GLOSSARY.md" })}
                      data-empty={row.country ? undefined : "true"}
                    >
                      <span className="flag" title={countryName(countries.data, row.country)}>
                        {row.flag}
                      </span>
                    </td>
                    <td
                      className="amount"
                      data-figure="true"
                      data-empty={row.opening_balance === null ? "true" : undefined}
                    >
                      {row.opening_balance === null ? (
                        <span className="muted">—</span>
                      ) : (
                        <span className={row.opening_balance < 0 ? "neg" : undefined}>
                          {amount(row.opening_balance, row.currency)}
                        </span>
                      )}
                    </td>
                    <td
                      className="small muted mono span-col"
                      data-label={t({ message: "Opening date", comment: "Column name shown beside a value on phones on the accounts import" })}
                      data-empty={row.opening_date ? undefined : "true"}
                    >
                      {row.opening_date ? formatDate(row.opening_date) : "—"}
                    </td>
                    <td
                      className="small mono"
                      data-label={t({ message: "IBAN", comment: "Column name shown beside a value on phones on the accounts import: International Bank Account Number; keep as is. See…" })}
                      data-empty={row.iban ? undefined : "true"}
                    >
                      {row.iban ?? <span className="muted">—</span>}
                    </td>
                    <td
                      className="small"
                      data-label={t({ message: "Problems", comment: "Column name shown beside a value on phones on the accounts import" })}
                      data-empty={row.problems.length ? undefined : "true"}
                    >
                      {row.problems.length === 0 ? (
                        <span className="muted">—</span>
                      ) : (
                        <ul className="plain-list neg" style={{ margin: 0 }}>
                          {row.problems.map((problem, at) => (
                            <li key={at}>{problem}</li>
                          ))}
                        </ul>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p />
          <button
            className="primary"
            disabled={!file || refused > 0 || commit.isPending || preview.isPending}
            onClick={() => file && commit.mutate(file)}
          >
            {plural(count, { one: `Import ${count} account`, other: `Import ${count} accounts` })}
          </button>
        </>
      )}
    </Panel>
  );
}
