/**
 * "Rates as of": how old the rates under a converted figure are (#396 §5.2).
 *
 * Nothing fetches rates on its own, so every page that shows a converted
 * figure says how old its rates are, in one line under the filter bar. It is
 * drawn from the `rates_status` block **in the same response as the figures**,
 * never from a call of its own, so the line and the numbers cannot disagree.
 *
 * - "As of" is the rate's own date; "fetched" is when someone last fetched.
 *   Both are shown: they answer different questions.
 * - The weakest link sets the date: with several currencies on screen it is
 *   the oldest of their newest rates. The "?" lists each one.
 * - Stale is a flag, not a block. The figures are still converted.
 * - It links to the Exchange rates page, where Fetch now is, and has no fetch
 *   button of its own.
 * - Hidden when nothing on the page was converted: a report in one currency.
 */

import { plural, t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { Hint } from "./bits";
import { formatDate } from "../lib/locale";
import { formatInstant } from "../lib/time";
import { type RatesStatus, sourceLabel } from "../lib/fx";

export type RatesState = "hidden" | "running" | "none" | "failed" | "stale" | "manual" | "fresh";

/** Which of the spec's states a status is in. Exported for its tests. */
export function ratesState(status: RatesStatus): RatesState {
  if (status.per_currency.length === 0 && status.not_converted.length === 0) return "hidden";
  if (status.running) return "running";
  if (status.per_currency.length === 0) return "none";
  if (status.last_fetch_failed) return "failed";
  if (status.stale) return "stale";
  if (status.per_currency.every((one) => one.source === "manual")) return "manual";
  return "fresh";
}

export function RatesAsOf({
  status,
  onOpen,
}: {
  status: RatesStatus;
  /** Opens the Exchange rates page. Left out on that page itself. */
  onOpen?: () => void;
}) {
  useLingui();
  const state = ratesState(status);
  if (state === "hidden") return null;

  const asOf = status.as_of ? formatDate(status.as_of) : "";
  const oldest = Math.max(0, ...status.per_currency.map((one) => one.age_days ?? 0));
  const sources = [...new Set(status.per_currency.map((one) => sourceLabel(one.source)))].join(" + ");
  const warn = state === "none" || state === "failed" || state === "stale" || status.not_converted.length > 0;

  let said: string;
  switch (state) {
    case "running":
      said = t({ message: "Fetching rates…", comment: "The Rates as of line while a fetch of exchange rates runs" });
      break;
    case "none":
      said = status.remote_in_chain
        ? t`No exchange rates yet · fetch them on the Exchange rates page`
        : t`No exchange rates yet · enter them on the Exchange rates page`;
      break;
    case "failed":
      said = t`Rates as of ${asOf} · the last fetch failed on ${formatInstant(status.last_fetch_failed!.at)}: ${status.last_fetch_failed!.error}`;
      break;
    case "stale":
      said = plural(oldest, {
        one: `Rates as of ${asOf}, # day old · fetch them on the Exchange rates page`,
        other: `Rates as of ${asOf}, # days old · fetch them on the Exchange rates page`,
      });
      break;
    case "manual":
      said =
        status.manual_changed_at && status.manual_changed_by
          ? t`Rates as of ${asOf} · entered by hand · last change ${formatInstant(status.manual_changed_at)} by ${status.manual_changed_by}`
          : t`Rates as of ${asOf} · entered by hand`;
      break;
    default:
      said =
        status.fetched_at && status.fetched_by
          ? t`Rates as of ${asOf} · ${sources} · fetched ${formatInstant(status.fetched_at)} by ${status.fetched_by}`
          : t`Rates as of ${asOf} · ${sources}`;
  }
  if (status.not_converted.length > 0) {
    const codes = status.not_converted.join(", ");
    said += " · " + t({ message: `${codes} not converted`, comment: "Added to the Rates as of line: currency codes with no exchange rate" });
  }

  return (
    <p className={`rates-as-of small${warn ? " warn" : ""}`} role="status">
      <span>{said}</span>
      {status.per_currency.length > 0 && (
        <Hint label={t({ message: "Rates as of", comment: "Name of the line saying how old the exchange rates on a page are" })}>
          {status.per_currency.map((one) => (
            <span key={one.currency} style={{ display: "block" }}>
              {one.currency}: {one.rate_date ? formatDate(one.rate_date) : "—"} · {sourceLabel(one.source)}
              {one.stale ? ` · ${t({ message: "stale", comment: "State of an exchange rate: older than the household's limit" })}` : ""}
            </span>
          ))}
        </Hint>
      )}
      {onOpen && (
        <>
          {" "}
          <button type="button" className="link" onClick={onOpen}>
            {t({ message: "Exchange rates", comment: "Link from the Rates as of line to the page of that name" })}
          </button>
        </>
      )}
    </p>
  );
}
