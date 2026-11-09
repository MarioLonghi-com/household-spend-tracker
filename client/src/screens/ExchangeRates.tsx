/**
 * Exchange rates: what the household chose, and what it has (#396 §3, §5.3).
 *
 * The settings page and the sync dashboard in one. Everything on it comes
 * from `GET …/fx/status`, read top to bottom: the line reports will show, the
 * headline tiles, Fetch now, one row per currency, a month strip, the
 * settings, the rates entered by hand, the fetch history, the stored rates,
 * and what no source covers.
 *
 * **The ledger never converts.** Nothing here is a converted amount; these
 * are the rates a report converts with. A rate is shown exactly as the server
 * sent it, a decimal string -- this screen does no arithmetic with one.
 *
 * Nothing fetches on its own. Rates arrive only when someone presses Fetch
 * now, and while one runs the page asks for the status again every second
 * until it has finished.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { ApiError, api } from "../lib/api";
import { Dialog, Field, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import { RatesAsOf } from "../components/RatesAsOf";
import { problemText } from "../lib/errorMessages";
import { formatCount, formatDate, monthLabel } from "../lib/locale";
import { formatInstant, localToday } from "../lib/time";
import {
  SOURCES,
  policyWord,
  sourceLabel,
  type FxCurrency,
  type FxFetch,
  type FxManualOut,
  type FxManualRow,
  type FxRate,
  type FxRates,
  type FxSettings,
  type FxStatus,
} from "../lib/fx";
import type { Household } from "../lib/types";
import { Browse, useBrowseView } from "./exchangeRates/Browse";

const count = (n: number) => formatCount(n);

function statusWord(status: FxCurrency["status"]): string {
  switch (status) {
    case "ok":
      return t({ message: "OK", comment: "State of a currency on the Exchange rates page: it has a fresh rate" });
    case "stale":
      return t({ message: "Stale", comment: "State of a currency on the Exchange rates page: its newest rate is older than the limit" });
    case "not_covered":
      return t({ message: "Not covered", comment: "State of a currency on the Exchange rates page: no source has a rate for it" });
    case "manual_only":
      return t({ message: "Manual only", comment: "State of a currency on the Exchange rates page: only rates entered by hand" });
  }
}

function outcomeWord(outcome: FxFetch["outcome"]): string {
  switch (outcome) {
    case "running":
      return t({ message: "Running", comment: "State of a fetch of exchange rates: still going" });
    case "ok":
      return t({ message: "Done", comment: "State of a fetch of exchange rates: it finished and stored what came back" });
    case "failed":
      return t({ message: "Failed", comment: "State of a fetch of exchange rates: it did not work" });
  }
}

function modeWord(mode: string): string {
  return mode === "range"
    ? t({ message: "Refetch a range", comment: "Kind of fetch of exchange rates: the dates given, replacing what is stored" })
    : t({ message: "Missing only", comment: "Kind of fetch of exchange rates: only the dates not stored yet" });
}

function seconds(ms: number | null): string {
  if (ms === null) return "—";
  return t({ message: `${(Math.round(ms / 100) / 10).toString()} s`, comment: "A duration in seconds, like 1.4 s" });
}

/** A refusal the server explained by code, in this language's words. */
function refusalText(code: string, english: string): string {
  return problemText(new ApiError(english, 422, undefined, { code, params: {} }));
}

export function ExchangeRates({ household }: { household: Household }) {
  const base = `/households/${household.id}/fx`;
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["fx-status", household.id],
    queryFn: ({ signal }) => api.get<FxStatus>(`${base}/status`, { signal }),
    // While a fetch runs, the page asks again every second until it is done.
    refetchInterval: (query) => (query.state.data?.running ? 1000 : false),
  });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["fx-status", household.id] });
    void client.invalidateQueries({ queryKey: ["fx-rates", household.id] });
  };

  // Shared between the per-currency rows and the forms below them.
  const [enterFor, setEnterFor] = useState<string | null>(null);
  const browse = useBrowseView(household.id);
  const [stripFor, setStripFor] = useState<string>("");
  const manualRef = useRef<HTMLElement>(null);
  const browseRef = useRef<HTMLElement>(null);

  const data = status.data;
  const strip = data?.currencies.find((one) => one.currency === stripFor) ?? data?.currencies[0];

  return (
    <>
      <h1>
        <Trans comment="Title of the Exchange rates page">Exchange rates</Trans>
      </h1>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          The ledger never converts: every amount stays in its account's currency. Reports may show
          figures in {household.base_currency}, the base currency, at the rates this page keeps, and
          every converted figure says which rate and date it used. Rates are fetched only when
          someone presses Fetch now.
        </Trans>
      </p>
      <Problem error={status.error} />
      {!data ? null : (
        <>
          <section className="card">
            <h2 className="section-title">
              <Trans comment="Heading on the Exchange rates page">What reports will say</Trans>
            </h2>
            {data.rates_status.per_currency.length === 0 && data.rates_status.not_converted.length === 0 ? (
              <p className="small muted">
                <Trans>Every account is in {household.base_currency}, so no report converts anything.</Trans>
              </p>
            ) : (
              <RatesAsOf status={data.rates_status} />
            )}
            <Tiles data={data} />
          </section>

          <FetchNow household={household} data={data} onDone={refresh} />

          <Currencies
            data={data}
            onEnter={(code) => {
              setEnterFor(code);
              manualRef.current?.scrollIntoView({ behavior: "smooth" });
            }}
            onBrowse={(code) => {
              browse.choose({ currency: code });
              browseRef.current?.scrollIntoView({ behavior: "smooth" });
            }}
            onMonths={setStripFor}
          />

          {strip && (
            <MonthStrip
              currencies={data.currencies}
              chosen={strip}
              onChoose={setStripFor}
            />
          )}

          {data.not_covered.length > 0 && (
            <section className="card">
              <h2 className="section-title">
                <Trans comment="Heading on the Exchange rates page: currencies no source has a rate for">Not covered</Trans>
              </h2>
              <ul className="plain-list">
                {data.not_covered.map((one) => (
                  <li key={one.currency} style={{ marginBottom: 8 }}>
                    <span className="mono">{one.currency}</span> ({one.accounts.map((a) => a.name).join(", ")}):{" "}
                    {one.reason === "not_iso" ? (
                      <Trans>
                        not a currency code ISO 4217 has assigned, so no source will ever publish a rate
                        for it. An account's currency cannot be changed: open a new account in the right
                        code and move its rows, or ask the operator for a one-off correction.
                      </Trans>
                    ) : (
                      <Trans>no source in the chain publishes a rate for it.</Trans>
                    )}{" "}
                    <button
                      type="button"
                      className="link"
                      onClick={() => {
                        setEnterFor(one.currency);
                        manualRef.current?.scrollIntoView({ behavior: "smooth" });
                      }}
                    >
                      <Trans>Enter a rate by hand</Trans>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <SettingsCard household={household} data={data} onSaved={refresh} />

          <section className="card" ref={manualRef}>
            <ManualRates
              household={household}
              data={data}
              enterFor={enterFor}
              onSaved={refresh}
            />
          </section>

          <History history={data.history} />

          <section className="card" ref={browseRef}>
            <Browse
              household={household}
              data={data}
              view={browse.view}
              onView={browse.setView}
              onChoose={browse.choose}
              fromAddress={browse.fromAddress}
            />
          </section>
        </>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- //
// The tiles
// --------------------------------------------------------------------------- //

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>
        <span className="stat-figure">{value}</span>
        {note ? <span className="muted small">{note}</span> : null}
      </dd>
    </div>
  );
}

function Tiles({ data }: { data: FxStatus }) {
  const tiles = data.tiles;
  const last = tiles.last_fetch;
  return (
    <dl className="stat-grid" style={{ marginTop: 12 }}>
      <Tile
        label={t({ message: "Last fetch", comment: "Label of a tile on the Exchange rates page" })}
        value={last ? formatInstant(last.finished_at ?? last.started_at) : t({ message: "Never", comment: "Value of the Last fetch tile when nobody has fetched rates" })}
        note={last ? `${outcomeWord(last.outcome)}${last.started_by ? ` · ${last.started_by}` : ""}` : undefined}
      />
      <Tile
        label={t({ message: "Rates as of", comment: "Label of a tile on the Exchange rates page: the date of the oldest newest rate" })}
        value={tiles.as_of ? formatDate(tiles.as_of) : "—"}
        note={plural(data.stale_days, { one: "stale after # day", other: "stale after # days" })}
      />
      <Tile
        label={t({ message: "Coverage", comment: "Label of a tile on the Exchange rates page: how many currencies have a rate" })}
        value={t({ message: `${count(tiles.covered)} of ${count(tiles.total)}`, comment: "Value of the Coverage tile: currencies with a rate, of those held" })}
        note={tiles.missing.length ? t({ message: `missing: ${tiles.missing.join(", ")}`, comment: "Under the Coverage tile: currency codes with no rate" }) : undefined}
      />
      <Tile
        label={t({ message: "Gaps", comment: "Label of a tile on the Exchange rates page: transaction dates with no recent rate" })}
        value={count(tiles.gaps)}
        note={t`dates with a transaction and no rate within the limit`}
      />
      <Tile
        label={t({ message: "Manual rates", comment: "Label of a tile on the Exchange rates page: rates entered by hand" })}
        value={count(tiles.manual_count)}
        note={
          tiles.manual_changed_at
            ? t({ message: `last change ${formatInstant(tiles.manual_changed_at)}${tiles.manual_changed_by ? ` · ${tiles.manual_changed_by}` : ""}`, comment: "Under the Manual rates tile: when they last changed, and who changed them" })
            : undefined
        }
      />
    </dl>
  );
}

// --------------------------------------------------------------------------- //
// Fetch now
// --------------------------------------------------------------------------- //

function FetchNow({ household, data, onDone }: { household: Household; data: FxStatus; onDone: () => void }) {
  const [rangeOpen, setRangeOpen] = useState(false);
  const [since, setSince] = useState("");
  const [until, setUntil] = useState(localToday());
  const [confirmRange, setConfirmRange] = useState(false);
  const [pressed, setPressed] = useState<string[] | null>(null);

  const start = useMutation({
    mutationFn: (body: { mode: "missing" | "range"; since?: string; until?: string }) =>
      api.post<FxFetch[]>(`/households/${household.id}/fx/fetch`, body),
    onSuccess: (rows) => {
      setPressed(rows.map((row) => row.id));
      onDone();
    },
  });

  const running = data.running;
  const mine = pressed ? data.history.filter((row) => pressed.includes(row.id)) : [];
  const finished = mine.length > 0 && mine.every((row) => row.outcome !== "running");
  const wasRunning = useRef(running);
  useEffect(() => {
    // The rates moved under every other section when a fetch ends.
    if (wasRunning.current && !running) onDone();
    wasRunning.current = running;
  }, [running, onDone]);

  return (
    <section className="card">
      <h2 className="section-title">
        <Trans comment="Heading on the Exchange rates page: the button that fetches rates, and what it does">Fetch now</Trans>
      </h2>
      {data.would_fetch.length > 0 ? (
        data.would_fetch.map((one) => (
          <p key={one.source} className="small">
            {t`Would fetch ${one.currencies.join(", ")} from ${sourceLabel(one.source)}, from ${formatDate(one.since)} to today.`}
          </p>
        ))
      ) : data.would_fetch_refused ? (
        <p className="small muted">{refusalText(data.would_fetch_refused, data.would_fetch_reason ?? "")}</p>
      ) : null}
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <button
          type="button"
          className="primary"
          disabled={running || start.isPending || data.would_fetch.length === 0}
          onClick={() => start.mutate({ mode: "missing" })}
        >
          <Trans comment="Button on the Exchange rates page: fetch the missing exchange rates now">Fetch now</Trans>
        </button>
        <button type="button" className="link" aria-expanded={rangeOpen} onClick={() => setRangeOpen((was) => !was)}>
          <Trans comment="Button on the Exchange rates page: open the controls to fetch a date range again">Refetch a range…</Trans>
        </button>
      </div>
      {rangeOpen && (
        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginTop: 8, alignItems: "flex-end" }}>
          <Field label={t({ message: "From", comment: "Label of a date field: the first day of a range to fetch" })}>
            <input type="date" value={since} max={until} onChange={(e) => setSince(e.target.value)} />
          </Field>
          <Field label={t({ message: "To", comment: "Label of a date field: the last day of a range to fetch" })}>
            <input type="date" value={until} max={localToday()} onChange={(e) => setUntil(e.target.value)} />
          </Field>
          <button type="button" disabled={!since || !until || running || start.isPending} onClick={() => setConfirmRange(true)}>
            <Trans comment="Button on the Exchange rates page: fetch the chosen date range again">Refetch</Trans>
          </button>
        </div>
      )}
      {confirmRange && (
        <Dialog title={t({ message: `Refetch ${formatDate(since)} to ${formatDate(until)}?`, comment: "Title of a confirmation: fetch the rates of these dates again" })} onClose={() => setConfirmRange(false)}>
          <p>
            <Trans>
              Every rate fetched for these dates replaces the one stored, and past converted figures
              that used it change. Rates entered by hand are not touched.
            </Trans>
          </p>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="primary"
              onClick={() => {
                setConfirmRange(false);
                start.mutate({ mode: "range", since, until });
              }}
            >
              <Trans comment="Button in a confirmation: yes, fetch the range again">Refetch</Trans>
            </button>
            <button type="button" onClick={() => setConfirmRange(false)}>
              <Trans comment="Button in a confirmation: do nothing">Cancel</Trans>
            </button>
          </div>
        </Dialog>
      )}
      <Problem error={start.error} />
      {running && (
        <p className="small" role="status">
          {data.running_fetches.length > 0
            ? data.running_fetches.map((row) =>
                t({ message: `Fetching ${sourceLabel(row.source)}, ${row.currencies.join(" ")} from ${formatDate(row.requested_since)}…`, comment: "While a fetch runs: the source, the currency codes, the first date" }),
              )
            : t`Another household on this instance is fetching rates. Wait for it to finish.`}
        </p>
      )}
      {!running && finished &&
        mine.map((row) =>
          row.outcome === "failed" ? (
            <p key={row.id} className="small neg" role="status">
              {t`The fetch failed: ${row.error ?? ""}`}
            </p>
          ) : (
            <p key={row.id} className="small" role="status">
              {t`Added ${count(row.rates_added)} rates, ${count(row.rates_unchanged)} unchanged, ${seconds(row.duration_ms)}.`}
            </p>
          ),
        )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Per currency
// --------------------------------------------------------------------------- //

type CurrencySort =
  | "currency"
  | "source"
  | "first"
  | "last"
  | "rates"
  | "age"
  | "coverage"
  | "gaps"
  | "status"
  | "own";

function Currencies({
  data,
  onEnter,
  onBrowse,
  onMonths,
}: {
  data: FxStatus;
  onEnter: (code: string) => void;
  onBrowse: (code: string) => void;
  onMonths: (code: string) => void;
}) {
  const { sort, direction, onSort } = useSort<CurrencySort>("currency");
  const rows = useMemo(
    () =>
      sortRows(data.currencies, sort, direction, (row, column) => {
        switch (column) {
          case "currency":
            return row.currency;
          case "source":
            return row.source_in_use ? sourceLabel(row.source_in_use) : null;
          case "first":
            return row.first_rate;
          case "last":
            return row.last_rate;
          case "rates":
            return row.rates;
          case "age":
            return row.age_days;
          case "coverage":
            return row.coverage_percent;
          case "gaps":
            return row.gaps;
          case "status":
            return statusWord(row.status);
          case "own":
            return row.own_rates;
        }
      }),
    [data.currencies, sort, direction],
  );
  const heading = { sort, direction, onSort };
  if (data.currencies.length === 0) {
    return (
      <section className="card">
        <p className="small muted">
          <Trans>Every account is in {data.base_currency}: there is no rate to keep.</Trans>
        </p>
      </section>
    );
  }
  return (
    <section className="card">
      <h2 className="section-title">
        <Trans comment="Heading on the Exchange rates page: one row per currency the household holds">Per currency</Trans>
      </h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <SortHeading label={t({ message: "Currency", comment: "Column heading on the Exchange rates page. See GLOSSARY.md" })} column="currency" {...heading} />
              <SortHeading label={t({ message: "Source in use", comment: "Column heading on the Exchange rates page: where today's rate comes from" })} column="source" {...heading} />
              <SortHeading label={t({ message: "First rate", comment: "Column heading on the Exchange rates page: date of the oldest stored rate" })} column="first" {...heading} />
              <SortHeading label={t({ message: "Last rate", comment: "Column heading on the Exchange rates page: date of the newest stored rate" })} column="last" {...heading} />
              <SortHeading label={t({ message: "Rates", comment: "Column heading on the Exchange rates page: how many rates are stored" })} column="rates" align="right" {...heading} />
              <SortHeading label={t({ message: "Age", comment: "Column heading on the Exchange rates page: days since the newest rate" })} column="age" align="right" {...heading} />
              <SortHeading label={t({ message: "Coverage", comment: "Column heading on the Exchange rates page: share of transaction dates with a rate" })} column="coverage" align="right" {...heading} />
              <SortHeading label={t({ message: "Gaps", comment: "Column heading on the Exchange rates page: transaction dates with no rate" })} column="gaps" align="right" {...heading} />
              <SortHeading label={t({ message: "Status", comment: "Column heading on the Exchange rates page" })} column="status" {...heading} />
              <SortHeading label={t({ message: "Own rates", comment: "Column heading on the Exchange rates page: days your own transfers give a rate" })} column="own" align="right" {...heading} />
              <th>
                <span className="sr-only">
                  <Trans comment="Column heading for buttons, read by screen readers only">Actions</Trans>
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.currency}>
                <td data-primary="true">
                  <span className="mono">{row.currency}</span>
                  <div className="small muted">{row.accounts.map((a) => a.name).join(", ")}</div>
                </td>
                <td className="small">{row.source_in_use ? sourceLabel(row.source_in_use) : "—"}</td>
                <td className="small mono">{row.first_rate ? formatDate(row.first_rate) : "—"}</td>
                <td className="small mono">{row.last_rate ? formatDate(row.last_rate) : "—"}</td>
                <td className="amount">{count(row.rates)}</td>
                <td className={`amount${row.status === "stale" ? " neg" : ""}`}>
                  {row.age_days === null ? "—" : plural(row.age_days, { one: "# day", other: "# days" })}
                </td>
                <td className="amount">{row.coverage_percent === null ? "—" : `${row.coverage_percent}%`}</td>
                <td className="amount">
                  {row.gaps > 0 ? (
                    <button type="button" className="link" onClick={() => onMonths(row.currency)}>
                      {count(row.gaps)}
                    </button>
                  ) : (
                    count(row.gaps)
                  )}
                </td>
                <td className={row.status === "ok" ? "small" : "small neg"}>{statusWord(row.status)}</td>
                <td className="amount">{count(row.own_rates)}</td>
                <td>
                  <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                    <button type="button" className="link" onClick={() => onEnter(row.currency)}>
                      <Trans comment="Button on the Exchange rates page: enter a rate for this currency by hand">Enter a rate</Trans>
                    </button>
                    <button type="button" className="link" onClick={() => onBrowse(row.currency)}>
                      <Trans comment="Button on the Exchange rates page: list the stored rates of this currency">Browse rates</Trans>
                    </button>
                    <button type="button" className="link" onClick={() => onMonths(row.currency)}>
                      <Trans comment="Button on the Exchange rates page: show the month strip of this currency">Months</Trans>
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MonthStrip({
  currencies,
  chosen,
  onChoose,
}: {
  currencies: FxCurrency[];
  chosen: FxCurrency;
  onChoose: (code: string) => void;
}) {
  const word = (state: string) =>
    state === "gap"
      ? t({ message: "transactions without a rate", comment: "A month on the strip: it has transactions and no rate within the limit" })
      : state === "rates"
        ? t({ message: "has rates", comment: "A month on the strip: covered by rates" })
        : t({ message: "nothing", comment: "A month on the strip: no transactions and no rates" });
  return (
    <section className="card">
      <h2 className="section-title">
        <Trans comment="Heading on the Exchange rates page: one cell per month, showing which have rates">Month by month</Trans>
      </h2>
      <Field label={t({ message: "Currency", comment: "Label of a picker on the Exchange rates page. See GLOSSARY.md" })}>
        <select value={chosen.currency} onChange={(e) => onChoose(e.target.value)}>
          {currencies.map((one) => (
            <option key={one.currency} value={one.currency}>
              {one.currency}
            </option>
          ))}
        </select>
      </Field>
      {chosen.months.length === 0 ? (
        <p className="small muted">
          <Trans>No transactions and no rates in {chosen.currency} yet.</Trans>
        </p>
      ) : (
        <ol className="fx-strip" aria-label={t({ message: `Months of ${chosen.currency}`, comment: "Name of the month strip, read by screen readers: {0} is a currency code" })}>
          {chosen.months.map((cell) => (
            <li
              key={cell.month}
              className={`fx-cell ${cell.state}`}
              title={`${monthLabel(cell.month)}: ${word(cell.state)}`}
              aria-label={`${monthLabel(cell.month)}: ${word(cell.state)}`}
            />
          ))}
        </ol>
      )}
      <p className="small muted fx-legend">
        <span className="fx-cell rates" aria-hidden="true" /> {word("rates")}{" "}
        <span className="fx-cell gap" aria-hidden="true" /> {word("gap")}{" "}
        <span className="fx-cell empty" aria-hidden="true" /> {word("empty")}
      </p>
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Settings
// --------------------------------------------------------------------------- //

function SettingsCard({ household, data, onSaved }: { household: Household; data: FxStatus; onSaved: () => void }) {
  const [chain, setChain] = useState<string[]>(data.chain);
  const [staleDays, setStaleDays] = useState(String(data.stale_days));
  const [policy, setPolicy] = useState(data.monthly_policy);
  const [showOwn, setShowOwn] = useState(data.show_own_rates);
  const [confirm, setConfirm] = useState(false);

  // Someone else's save, noticed by the next read: start again from it.
  const saved = `${data.chain.join(",")}|${data.stale_days}|${data.monthly_policy}|${data.show_own_rates}`;
  useEffect(() => {
    setChain(data.chain);
    setStaleDays(String(data.stale_days));
    setPolicy(data.monthly_policy);
    setShowOwn(data.show_own_rates);
  }, [saved]);

  const save = useMutation({
    mutationFn: () =>
      api.put<FxSettings>(`/households/${household.id}/fx/settings`, {
        chain,
        stale_days: Number(staleDays),
        monthly_policy: policy,
        show_own_rates: showOwn,
      }),
    onSuccess: onSaved,
  });

  const changesFigures = chain.join(",") !== data.chain.join(",") || policy !== data.monthly_policy;
  const move = (at: number, by: number) => {
    const next = [...chain];
    const [one] = next.splice(at, 1);
    next.splice(at + by, 0, one);
    setChain(next);
  };
  const unused = SOURCES.filter((one) => !chain.includes(one));

  return (
    <section className="card config-card">
      <h2 className="section-title">
        <Trans comment="Heading on the Exchange rates page">Settings</Trans>
      </h2>
      <p className="small muted">
        <Trans>
          Rate sources, in order: the first with a rate within the stale limit is used. The base
          currency is set on the household page.
        </Trans>
      </p>
      <ol className="plain-list">
        {chain.map((source, at) => (
          <li key={source} className="row" style={{ gap: 6, alignItems: "center", marginBottom: 4 }}>
            <span>
              {at + 1}. {sourceLabel(source)}
            </span>
            <button type="button" className="link" disabled={at === 0} onClick={() => move(at, -1)}>
              <Trans comment="Button: move this rate source one place earlier in the list">Up</Trans>
            </button>
            <button type="button" className="link" disabled={at === chain.length - 1} onClick={() => move(at, 1)}>
              <Trans comment="Button: move this rate source one place later in the list">Down</Trans>
            </button>
            <button type="button" className="link" onClick={() => setChain(chain.filter((one) => one !== source))}>
              <Trans comment="Button: take this rate source out of the list">Remove</Trans>
            </button>
          </li>
        ))}
      </ol>
      {chain.length === 0 && (
        <p className="small neg">
          <Trans>With no source, nothing converts: every report shows one currency at a time.</Trans>
        </p>
      )}
      {unused.length > 0 && (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          {unused.map((source) => (
            <button key={source} type="button" onClick={() => setChain([...chain, source])}>
              {t({ message: `Add ${sourceLabel(source)}`, comment: "Button: add this rate source to the end of the list" })}
            </button>
          ))}
        </div>
      )}
      {chain.includes("own") && (
        <p className="small neg">
          <Trans>
            Your own transfers' rates include the bank's spread and fees, and exist only on the days
            you exchanged money, so neighbouring months may use different kinds of rate.
          </Trans>
        </p>
      )}
      <Field label={t({ message: "Treat a rate as stale after (days)", comment: "Label of a number field on the Exchange rates page" })}>
        <input type="number" min={1} max={365} value={staleDays} onChange={(e) => setStaleDays(e.target.value)} />
      </Field>
      <Field label={t({ message: "Rate used for monthly figures", comment: "Label of a picker on the Exchange rates page" })}>
        <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
          {["month_avg", "month_end", "spot"].map((one) => (
            <option key={one} value={one}>
              {policyWord(one)}
            </option>
          ))}
        </select>
      </Field>
      <label className="row" style={{ gap: 6 }}>
        <input type="checkbox" checked={showOwn} onChange={(e) => setShowOwn(e.target.checked)} />
        <Trans>Show my own rates next to the rate in use</Trans>
      </label>
      <Problem error={save.error} />
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <button type="button" className="primary" disabled={save.isPending} onClick={() => (changesFigures ? setConfirm(true) : save.mutate())}>
          <Trans comment="Button on the Exchange rates page: keep the settings">Save settings</Trans>
        </button>
        {save.isSuccess && !save.isPending && (
          <span className="small muted" role="status">
            <Trans comment="Said after the settings were kept">Saved.</Trans>
          </span>
        )}
      </div>
      {confirm && (
        <Dialog title={t`Change where rates come from?`} onClose={() => setConfirm(false)}>
          <p>
            <Trans>
              Past converted figures will change: every report converts with the new choice from its
              next load. The change is written to History.
            </Trans>
          </p>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="primary"
              onClick={() => {
                setConfirm(false);
                save.mutate();
              }}
            >
              <Trans comment="Button in a confirmation: yes, change the rate settings">Change it</Trans>
            </button>
            <button type="button" onClick={() => setConfirm(false)}>
              <Trans comment="Button in a confirmation: do nothing">Cancel</Trans>
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Rates entered by hand
// --------------------------------------------------------------------------- //

type ManualSort = "currency" | "date" | "rate" | "inverse";
type PreviewSort = "line" | "currency" | "date" | "rate" | "problems";

function ManualRates({
  household,
  data,
  enterFor,
  onSaved,
}: {
  household: Household;
  data: FxStatus;
  enterFor: string | null;
  onSaved: () => void;
}) {
  const path = `/households/${household.id}/fx/rates`;
  const [currency, setCurrency] = useState(enterFor ?? data.currencies[0]?.currency ?? "");
  const [period, setPeriod] = useState<"day" | "month">("day");
  const [day, setDay] = useState(localToday());
  const [rate, setRate] = useState("");
  const [deleting, setDeleting] = useState<FxRate | null>(null);
  const [file, setFile] = useState<File | null>(null);
  useEffect(() => {
    if (enterFor) setCurrency(enterFor);
  }, [enterFor]);

  const typedDate = period === "month" ? day.slice(0, 7) : day;
  // The server's own verdict as you type, so the inverse shown is its.
  const check = useQuery({
    queryKey: ["fx-manual-check", household.id, currency, typedDate, rate],
    queryFn: () => api.post<FxManualOut>(`${path}/manual`, { currency, date: typedDate, rate, dry_run: true }),
    enabled: Boolean(currency && typedDate && rate),
    retry: false,
  });
  const checked: FxManualRow | undefined = check.data?.rows[0];

  const add = useMutation({
    mutationFn: () => api.post<FxManualOut>(`${path}/manual`, { currency, date: typedDate, rate }),
    onSuccess: () => {
      setRate("");
      onSaved();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`${path}/manual/${id}`),
    onSuccess: () => {
      setDeleting(null);
      onSaved();
    },
  });

  const send = (chosen: File, dryRun: boolean) => {
    const form = new FormData();
    form.append("file", chosen);
    form.append("dry_run", dryRun ? "true" : "false");
    return api.upload<FxManualOut>(`${path}/manual/import`, form);
  };
  const preview = useMutation({ mutationFn: (chosen: File) => send(chosen, true) });
  const keep = useMutation({
    mutationFn: (chosen: File) => send(chosen, false),
    onSuccess: () => {
      setFile(null);
      preview.reset();
      onSaved();
    },
  });

  const listed = useQuery({
    queryKey: ["fx-rates", household.id, "manual"],
    queryFn: ({ signal }) => api.get<FxRates>(`${path}?source=manual&limit=5000`, { signal }),
  });
  const { sort, direction, onSort } = useSort<ManualSort>("date", "desc");
  const manual = useMemo(
    () =>
      sortRows(listed.data?.rates ?? [], sort, direction, (row, column) => {
        switch (column) {
          case "currency":
            return row.currency;
          case "date":
            return row.date;
          case "rate":
            return Number(row.rate);
          case "inverse":
            return row.rate_in_base === null ? null : Number(row.rate_in_base);
        }
      }),
    [listed.data, sort, direction],
  );
  const heading = { sort, direction, onSort };

  const previewSort = useSort<PreviewSort>("line");
  const previewRows = useMemo(
    () =>
      sortRows(preview.data?.rows ?? [], previewSort.sort, previewSort.direction, (row, column) => {
        switch (column) {
          case "line":
            return row.line;
          case "currency":
            return row.currency;
          case "date":
            return row.date;
          case "rate":
            return Number(row.rate) || null;
          case "problems":
            return row.problems.join(" ");
        }
      }),
    [preview.data, previewSort.sort, previewSort.direction],
  );
  const refused = preview.data?.rows.filter((row) => row.problems.length > 0).length ?? 0;

  return (
    <>
      <h2 className="section-title">
        <Trans comment="Heading on the Exchange rates page: rates a member typed or imported">Rates entered by hand</Trans>
      </h2>
      <p className="small muted">
        <Trans>
          A rate is units of the currency per 1 {household.base_currency}, the way the ECB quotes
          it. A monthly rate covers every day of its month.
        </Trans>
      </p>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t({ message: "Currency", comment: "Label of a field on the Exchange rates page. See GLOSSARY.md" })}>
          <input
            value={currency}
            maxLength={3}
            size={5}
            list="fx-currencies"
            onChange={(e) => setCurrency(e.target.value.toUpperCase())}
          />
        </Field>
        <datalist id="fx-currencies">
          {data.currencies.map((one) => (
            <option key={one.currency} value={one.currency} />
          ))}
        </datalist>
        <Field label={t({ message: "For", comment: "Label of a picker: whether a rate is for one day or a whole month" })}>
          <select value={period} onChange={(e) => setPeriod(e.target.value as "day" | "month")}>
            <option value="day">{t({ message: "A day", comment: "Option: a rate for one day" })}</option>
            <option value="month">{t({ message: "A month", comment: "Option: a rate for a whole month" })}</option>
          </select>
        </Field>
        <Field label={period === "month" ? t({ message: "Month", comment: "Label of a field: the month a rate is for" }) : t({ message: "Date", comment: "Label of a field: the day a rate is for" })}>
          {period === "month" ? (
            <input type="month" value={day.slice(0, 7)} max={localToday().slice(0, 7)} onChange={(e) => setDay(`${e.target.value}-01`)} />
          ) : (
            <input type="date" value={day} max={localToday()} onChange={(e) => setDay(e.target.value)} />
          )}
        </Field>
        <Field label={t({ message: `Rate: ${currency || "…"} per 1 ${household.base_currency}`, comment: "Label of the rate field: how many units of the first currency one unit of the base buys" })}>
          <input inputMode="decimal" value={rate} size={12} onChange={(e) => setRate(e.target.value.trim())} />
        </Field>
        <button
          type="button"
          className="primary"
          disabled={!checked || checked.problems.length > 0 || checked.unchanged || add.isPending}
          onClick={() => add.mutate()}
        >
          {checked?.replaces
            ? t({ message: "Replace the rate", comment: "Button: keep this rate in place of the one stored for that date" })
            : t({ message: "Add the rate", comment: "Button: keep this exchange rate" })}
        </button>
      </div>
      {checked && checked.problems.length === 0 && checked.inverse && (
        <p className="small" role="status">
          {t`1 ${checked.currency} = ${checked.inverse} ${household.base_currency}`}
          {checked.replaces ? ` · ${t({ message: `replaces ${checked.replaces}`, comment: "Beside a rate being entered: the rate stored for that date, which it would replace" })}` : ""}
          {checked.unchanged ? ` · ${t({ message: "already stored", comment: "Beside a rate being entered: the same rate is stored for that date" })}` : ""}
        </p>
      )}
      {checked && checked.problems.length > 0 && (
        <ul className="plain-list neg small">
          {checked.problems.map((one, at) => (
            <li key={at}>{one}</li>
          ))}
        </ul>
      )}
      <Problem error={add.error} />

      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Heading on the Exchange rates page: many rates from one CSV file">From a file</Trans>
      </h3>
      <p className="small">
        <Trans>
          Columns <span className="mono">currency,date,rate</span>: a date as{" "}
          <span className="mono">YYYY-MM-DD</span>, or a month as <span className="mono">YYYY-MM</span>.
          All or none: one line with a problem keeps the whole file out.{" "}
          <a href={`/api${path}/manual/template.csv`} download>
            Download the template
          </a>
          .
        </Trans>
      </p>
      <Field label={t({ message: "Rates file", comment: "Label of a file field on the Exchange rates page" })}>
        <input
          type="file"
          accept=".csv,text/csv"
          disabled={keep.isPending}
          onChange={(e) => {
            const chosen = e.target.files?.[0] ?? null;
            setFile(chosen);
            keep.reset();
            if (chosen) preview.mutate(chosen);
            else preview.reset();
            e.target.value = "";
          }}
        />
      </Field>
      <Problem error={preview.error ?? keep.error} />
      {preview.data && file && (
        <>
          <p className="small">
            {refused === 0
              ? plural(preview.data.rows.length, {
                  one: "# rate ready to save. Nothing has been saved yet.",
                  other: "# rates ready to save. Nothing has been saved yet.",
                })
              : plural(preview.data.rows.length, {
                  one: `${refused} of # line has a problem, so nothing can be saved yet. Correct the file and choose it again.`,
                  other: `${refused} of # lines have a problem, so nothing can be saved yet. Correct the file and choose it again.`,
                })}
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading label={t({ message: "Line", comment: "Column heading on the rates import: noun, a line of a file" })} column="line" align="right" {...previewSort} />
                  <SortHeading label={t({ message: "Currency", comment: "Column heading on the rates import. See GLOSSARY.md" })} column="currency" {...previewSort} />
                  <SortHeading label={t({ message: "Date", comment: "Column heading on the rates import: a day or a month" })} column="date" {...previewSort} />
                  <SortHeading label={t({ message: "Rate", comment: "Column heading on the rates import: an exchange rate" })} column="rate" align="right" {...previewSort} />
                  <SortHeading label={t({ message: "Problems", comment: "Column heading on the rates import" })} column="problems" {...previewSort} />
                </tr>
              </thead>
              <tbody>
                {previewRows.map((row) => (
                  <tr key={row.line}>
                    <td className="amount muted">{row.line}</td>
                    <td className="mono">{row.currency}</td>
                    <td className="mono small">{row.date}</td>
                    <td className="amount mono">{row.rate}</td>
                    <td className="small">
                      {row.problems.length === 0 ? (
                        <span className="muted">{row.replaces ? t({ message: `replaces ${row.replaces}`, comment: "Beside a rate being entered: the rate stored for that date, which it would replace" }) : "—"}</span>
                      ) : (
                        <ul className="plain-list neg" style={{ margin: 0 }}>
                          {row.problems.map((one, at) => (
                            <li key={at}>{one}</li>
                          ))}
                        </ul>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button type="button" className="primary" disabled={refused > 0 || keep.isPending} onClick={() => keep.mutate(file)}>
            <Trans comment="Button on the Exchange rates page: save every rate in the file">Save these rates</Trans>
          </button>
        </>
      )}
      {keep.data && (
        <p className="small" role="status">
          {t`Saved: ${count(keep.data.added)} added, ${count(keep.data.replaced)} replaced, ${count(keep.data.unchanged)} unchanged.`}
        </p>
      )}

      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Heading on the Exchange rates page: the list of rates entered by hand">Stored</Trans>
      </h3>
      {manual.length === 0 ? (
        <p className="small muted">
          <Trans>No rates entered by hand.</Trans>
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label={t({ message: "Currency", comment: "Column heading on the Exchange rates page. See GLOSSARY.md" })} column="currency" {...heading} />
                <SortHeading label={t({ message: "Date", comment: "Column heading on the Exchange rates page: the day or month a rate is for" })} column="date" {...heading} />
                <SortHeading label={t({ message: "Rate", comment: "Column heading on the Exchange rates page: an exchange rate as entered" })} column="rate" align="right" {...heading} />
                <SortHeading label={t({ message: `Per 1 ${household.base_currency}`, comment: "Column heading: the rate as units per one unit of the base currency" })} column="inverse" align="right" {...heading} />
                <th>
                  <span className="sr-only">
                    <Trans comment="Column heading for buttons, read by screen readers only">Actions</Trans>
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              {manual.map((row) => (
                <tr key={row.id ?? `${row.currency}${row.date}`}>
                  <td className="mono">{row.currency}</td>
                  <td className="mono small">
                    {row.period === "month" ? monthLabel(row.date.slice(0, 7)) : formatDate(row.date)}
                  </td>
                  <td className="amount mono">
                    {row.rate}
                    {row.quote_currency !== household.base_currency ? ` / ${row.quote_currency}` : ""}
                  </td>
                  <td className="amount mono">{row.rate_in_base ?? "—"}</td>
                  <td>
                    <button type="button" className="link" onClick={() => setDeleting(row)}>
                      <Trans comment="Button on the Exchange rates page: delete this rate">Delete</Trans>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {deleting && (
        <Dialog title={t`Delete the ${deleting.currency} rate of ${formatDate(deleting.date)}?`} onClose={() => setDeleting(null)}>
          <p style={{ marginTop: 0 }}>
            <Trans>Undo in History brings it back.</Trans>
          </p>
          <Problem error={remove.error} />
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="danger" disabled={remove.isPending} onClick={() => remove.mutate(deleting.id!)}>
              <Trans comment="Button in a confirmation: yes, delete the rate">Delete</Trans>
            </button>
            <button type="button" onClick={() => setDeleting(null)}>
              <Trans comment="Button in a confirmation: do nothing">Cancel</Trans>
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- //
// Fetch history
// --------------------------------------------------------------------------- //

type HistorySort = "when" | "by" | "source" | "mode" | "range" | "added" | "unchanged" | "duration" | "outcome";

function History({ history }: { history: FxFetch[] }) {
  const { sort, direction, onSort } = useSort<HistorySort>("when", "desc");
  const rows = useMemo(
    () =>
      sortRows(history, sort, direction, (row, column) => {
        switch (column) {
          case "when":
            return row.started_at;
          case "by":
            return row.started_by;
          case "source":
            return sourceLabel(row.source);
          case "mode":
            return modeWord(row.mode);
          case "range":
            return row.requested_since;
          case "added":
            return row.rates_added;
          case "unchanged":
            return row.rates_unchanged;
          case "duration":
            return row.duration_ms;
          case "outcome":
            return outcomeWord(row.outcome);
        }
      }),
    [history, sort, direction],
  );
  const heading = { sort, direction, onSort };
  return (
    <section className="card">
      <h2 className="section-title">
        <Trans comment="Heading on the Exchange rates page: past presses of Fetch now">Fetch history</Trans>
      </h2>
      {history.length === 0 ? (
        <p className="small muted">
          <Trans>Nobody has fetched rates for this household yet.</Trans>
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label={t({ message: "When", comment: "Column heading on the fetch history: when the fetch started" })} column="when" {...heading} />
                <SortHeading label={t({ message: "By", comment: "Column heading on the fetch history: who pressed Fetch now" })} column="by" {...heading} />
                <SortHeading label={t({ message: "Source", comment: "Column heading on the fetch history: where rates were fetched from" })} column="source" {...heading} />
                <SortHeading label={t({ message: "Mode", comment: "Column heading on the fetch history: missing only, or a range again" })} column="mode" {...heading} />
                <SortHeading label={t({ message: "Dates asked for", comment: "Column heading on the fetch history: the date range and currencies requested" })} column="range" {...heading} />
                <SortHeading label={t({ message: "Added", comment: "Column heading on the fetch history: rates newly stored" })} column="added" align="right" {...heading} />
                <SortHeading label={t({ message: "Unchanged", comment: "Column heading on the fetch history: rates already stored" })} column="unchanged" align="right" {...heading} />
                <SortHeading label={t({ message: "Took", comment: "Column heading on the fetch history: how long the fetch took" })} column="duration" align="right" {...heading} />
                <SortHeading label={t({ message: "Outcome", comment: "Column heading on the fetch history: done, failed or running" })} column="outcome" {...heading} />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="small">{formatInstant(row.started_at)}</td>
                  <td className="small">{row.started_by ?? "—"}</td>
                  <td className="small">{sourceLabel(row.source)}</td>
                  <td className="small">{modeWord(row.mode)}</td>
                  <td className="small mono">
                    {formatDate(row.requested_since)} – {formatDate(row.requested_until)}
                    <div className="muted">{row.currencies.join(" ")}</div>
                  </td>
                  <td className="amount">{count(row.rates_added)}</td>
                  <td className="amount">{count(row.rates_unchanged)}</td>
                  <td className="amount">{seconds(row.duration_ms)}</td>
                  <td className={row.outcome === "failed" ? "small neg" : "small"}>
                    {outcomeWord(row.outcome)}
                    {row.error ? <div>{row.error}</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
