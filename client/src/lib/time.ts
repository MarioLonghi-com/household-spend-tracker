import { formatLocale } from "./locale";

/**
 * One place that turns a server timestamp into local text.
 *
 * Every datetime the server sends is naive UTC (`app/models/base.py` strips the
 * tzinfo), so the string carries no offset and `new Date(...)` reads it as
 * local time -- two hours adrift in Madrid, and in the flattering direction: an
 * invitation looks like it expires later than it does. Appending the `Z` is the
 * whole fix, and it belongs in one function rather than at each call site,
 * because the two call sites had already drifted apart.
 */
export function asInstant(serverTimestamp: string): Date {
  const text = serverTimestamp.trim();
  const alreadyMarked = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text);
  return new Date(alreadyMarked ? text : `${text}Z`);
}

/** Date and time, in the reader's own zone and format. */
export function formatInstant(serverTimestamp: string): string {
  const when = asInstant(serverTimestamp);
  return Number.isNaN(when.getTime()) ? serverTimestamp : when.toLocaleString(formatLocale());
}

/**
 * Today on this device's calendar, as YYYY-MM-DD. Not `toISOString()`, which
 * is UTC: between midnight and two in Madrid that is still yesterday, while the
 * server's `date.today()` -- which refuses dates in the future -- is local.
 * Every screen that means "today" asks here (#23).
 */
export function localToday(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
