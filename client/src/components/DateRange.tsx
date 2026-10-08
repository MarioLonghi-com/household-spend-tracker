/**
 * The register's date filter: five presets, and a month/year range behind them.
 *
 * The presets are the quick part and are always visible -- most of the time the
 * question is "this month" or "this year" and that should be one click, not a
 * dialog. The exact range is there for the times it isn't.
 *
 * There is no Apply button on purpose. Everything here applies as you pick it,
 * so the filter and what you are looking at can never disagree.
 */

import { useMemo, useRef } from "react";
import type { KeyboardEvent } from "react";
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { monthName } from "../lib/locale";

export type Range = { since: string | null; until: string | null };

export const ALL_DATES: Range = { since: null, until: null };

/** January to December, in the language the words are in (`lib/locale.ts`). */
const MONTH_INDEXES = Array.from({ length: 12 }, (_, index) => index);

/**
 * The month as a number, for the exact range.
 *
 * The presets and the exact pickers sit on one line now, and "September" is
 * four times the width of "09" for the same fact -- twice over, because there
 * is a From and a To. The number is what a date in this app is written with
 * anyway (`2026-09-21`), so the compact spelling is also the one the rest of
 * the screen uses.
 *
 * The *name* is still what the control is called: the option keeps its
 * `aria-label`-shaped title and the select its own label, so a screen reader
 * and a hover both say "September" rather than reading out a number.
 */
function monthNumber(index: number): string {
  return String(index + 1).padStart(2, "0");
}

/** Local, not UTC: `toISOString` would move a date across the line near midnight. */
function iso(year: number, monthIndex: number, day: number): string {
  return `${year}-${String(monthIndex + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function lastDayOf(year: number, monthIndex: number): number {
  return new Date(year, monthIndex + 1, 0).getDate();
}

function startOfMonth(year: number, monthIndex: number): string {
  return iso(year, monthIndex, 1);
}

function endOfMonth(year: number, monthIndex: number): string {
  return iso(year, monthIndex, lastDayOf(year, monthIndex));
}

/**
 * The window ending with this month and running `months` months back.
 *
 * Inclusive of the current month, so "Latest 6 months" is this month and the
 * five before it -- which is what a person counting on their fingers means,
 * and what makes the report's month columns come to six rather than seven.
 */
export function monthsBack(months: number, today = new Date()): Range {
  const y = today.getFullYear();
  const m = today.getMonth();
  const start = new Date(y, m - (months - 1), 1);
  return {
    since: startOfMonth(start.getFullYear(), start.getMonth()),
    until: endOfMonth(y, m),
  };
}

/**
 * Which preset a screen can leave out, by what it is rather than by its label.
 *
 * A key rather than the words on the chip, so rewording a chip cannot quietly
 * put back one a screen had dropped.
 */
export type PresetKey =
  | "month"
  | "3-months"
  | "6-months"
  | "12-months"
  | "year"
  | "last-year"
  | "all";

export function presetRanges(
  today = new Date(),
): { key: PresetKey; label: string; range: Range }[] {
  const y = today.getFullYear();
  const m = today.getMonth();
  return [
    { key: "month", label: t({ message: "This month", comment: "Label on the date range picker" }), range: { since: startOfMonth(y, m), until: endOfMonth(y, m) } },
    { key: "3-months", label: t({ message: "Latest 3 months", comment: "Label on the date range picker" }), range: monthsBack(3, today) },
    { key: "6-months", label: t({ message: "Latest 6 months", comment: "Label on the date range picker" }), range: monthsBack(6, today) },
    { key: "12-months", label: t({ message: "Latest 12 months", comment: "Label on the date range picker" }), range: monthsBack(12, today) },
    { key: "year", label: t({ message: "This year", comment: "Label on the date range picker" }), range: { since: iso(y, 0, 1), until: iso(y, 11, 31) } },
    { key: "last-year", label: t({ message: "Last year", comment: "Label on the date range picker" }), range: { since: iso(y - 1, 0, 1), until: iso(y - 1, 11, 31) } },
    { key: "all", label: t({ message: "All dates", comment: "Label on the date range picker" }), range: ALL_DATES },
  ];
}

/**
 * The same window moved one length earlier (-1) or later (+1), or null.
 *
 * What the ‹ and › steppers do (#123): "This month" steps to last month,
 * "Latest 6 months" to the six before those, a year to the year before. The
 * length is counted in whole months, which every range this control can make
 * is -- the presets and the exact pickers both land on month boundaries.
 *
 * Null when either end is open: "everything before March" has no length to
 * step by, and guessing one would move the filter somewhere nobody asked.
 */
export function stepRange(value: Range, direction: -1 | 1): Range | null {
  if (!value.since || !value.until) return null;
  const from = new Date(`${value.since}T00:00:00`);
  const to = new Date(`${value.until}T00:00:00`);
  const span =
    (to.getFullYear() - from.getFullYear()) * 12 + (to.getMonth() - from.getMonth()) + 1;
  if (span < 1) return null;
  const start = new Date(from.getFullYear(), from.getMonth() + direction * span, 1);
  const end = new Date(to.getFullYear(), to.getMonth() + direction * span, 1);
  return {
    since: startOfMonth(start.getFullYear(), start.getMonth()),
    until: endOfMonth(end.getFullYear(), end.getMonth()),
  };
}

/**
 * Arrow keys move along a row of controls, as well as Tab.
 *
 * Tab still visits every one -- that is what #123 asked for, and nothing here
 * takes a control out of the tab order. The arrows are the faster way along
 * a row of seven chips once you are in it, and Home and End jump to its ends.
 * Selects are left out of the arrows on purpose: on a closed select they
 * already change the month, and stealing them would break that.
 */
function moveAlong(event: KeyboardEvent<HTMLElement>, holder: HTMLElement | null) {
  const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
  if (!holder || !keys.includes(event.key)) return;
  const target = event.target as HTMLElement;
  if (target.tagName === "SELECT") return;
  const stops = [...holder.querySelectorAll<HTMLElement>("button:not(:disabled)")];
  const at = stops.indexOf(target);
  if (at < 0) return;
  const next =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? stops.length - 1
        : Math.min(Math.max(at + (event.key === "ArrowRight" ? 1 : -1), 0), stops.length - 1);
  event.preventDefault();
  stops[next].focus();
}

function same(a: Range, b: Range): boolean {
  return a.since === b.since && a.until === b.until;
}

export function DateRange({
  value,
  onChange,
  omit = [],
}: {
  value: Range;
  onChange: (next: Range) => void;
  /** Presets this screen does without. The register drops "Latest 3 months" (#123). */
  omit?: PresetKey[];
}) {
  // The preset labels are words: rebuilt when the language changes.
  const { i18n } = useLingui();
  const omitted = omit.join(",");
  const presets = useMemo(
    () => presetRanges().filter((preset) => !omitted.split(",").includes(preset.key)),
    [omitted, i18n.locale],
  );
  const holder = useRef<HTMLDivElement>(null);
  const earlier = stepRange(value, -1);
  const later = stepRange(value, 1);
  const thisYear = new Date().getFullYear();
  const years = useMemo(
    () => Array.from({ length: 16 }, (_, i) => thisYear + 1 - i),
    [thisYear],
  );

  // Which end the pickers show when that end is open. A blank "from" means
  // "since forever", so the picker starts somewhere sensible rather than empty.
  const from = value.since ? new Date(`${value.since}T00:00:00`) : null;
  const until = value.until ? new Date(`${value.until}T00:00:00`) : null;

  const setFrom = (year: number, monthIndex: number) =>
    onChange({ ...value, since: startOfMonth(year, monthIndex) });
  const setUntil = (year: number, monthIndex: number) =>
    onChange({ ...value, until: endOfMonth(year, monthIndex) });

  return (
    <div
      className="daterange"
      ref={holder}
      onKeyDown={(event) => moveAlong(event, holder.current)}
    >
      <div className="daterange-presets" role="group" aria-label={t({ message: "Date range", comment: "Screen-reader name on the date range picker" })}>
        {presets.map((preset) => {
          const active = same(value, preset.range);
          return (
            <button
              key={preset.key}
              type="button"
              className={active ? "chip active" : "chip"}
              aria-pressed={active}
              onClick={() => onChange(preset.range)}
            >
              {preset.label}
            </button>
          );
        })}
      </div>

      <div className="daterange-exact">
        {/* Tap through the periods one at a time, or Tab to these and press
            Enter: the same window, one length earlier or later. */}
        <button
          type="button"
          className="chip daterange-step"
          aria-label={t`Earlier: the same length of time, just before`}
          title={t({ message: "Earlier", comment: "Tooltip on the date range picker" })}
          disabled={!earlier}
          onClick={() => earlier && onChange(earlier)}
        >
          ‹
        </button>
        <span className="daterange-label">{t({ message: "From", comment: "Text on the date range picker: the start of a range, or where money comes from" })}</span>
        <select
          aria-label={t({ message: "From month", comment: "Screen-reader name on the date range picker" })}
          value={from ? from.getMonth() : ""}
          onChange={(e) =>
            setFrom(from ? from.getFullYear() : thisYear, Number(e.target.value))
          }
        >
          <option value="" disabled>
            —
          </option>
          {MONTH_INDEXES.map((index) => (
            <option key={index} value={index} title={monthName(index)}>
              {monthNumber(index)}
            </option>
          ))}
        </select>
        <select
          aria-label={t({ message: "From year", comment: "Screen-reader name on the date range picker" })}
          value={from ? from.getFullYear() : ""}
          onChange={(e) => setFrom(Number(e.target.value), from ? from.getMonth() : 0)}
        >
          <option value="" disabled>
            —
          </option>
          {years.map((year) => (
            <option key={year} value={year}>
              {year}
            </option>
          ))}
        </select>

        <span className="daterange-label">{t({ message: "To", comment: "Text on the date range picker: the end of a range, or where money goes" })}</span>
        <select
          aria-label={t({ message: "To month", comment: "Screen-reader name on the date range picker" })}
          value={until ? until.getMonth() : ""}
          onChange={(e) =>
            setUntil(until ? until.getFullYear() : thisYear, Number(e.target.value))
          }
        >
          <option value="" disabled>
            —
          </option>
          {MONTH_INDEXES.map((index) => (
            <option key={index} value={index} title={monthName(index)}>
              {monthNumber(index)}
            </option>
          ))}
        </select>
        <select
          aria-label={t({ message: "To year", comment: "Screen-reader name on the date range picker" })}
          value={until ? until.getFullYear() : ""}
          onChange={(e) => setUntil(Number(e.target.value), until ? until.getMonth() : 11)}
        >
          <option value="" disabled>
            —
          </option>
          {years.map((year) => (
            <option key={year} value={year}>
              {year}
            </option>
          ))}
        </select>

        <button
          type="button"
          className="chip daterange-step"
          aria-label={t`Later: the same length of time, just after`}
          title={t({ message: "Later", comment: "Tooltip on the date range picker" })}
          disabled={!later}
          onClick={() => later && onChange(later)}
        >
          ›
        </button>

        {(value.since || value.until) && (
          <button type="button" className="link" onClick={() => onChange(ALL_DATES)}>
            Clear
          </button>
        )}
      </div>
    </div>
  );
}
