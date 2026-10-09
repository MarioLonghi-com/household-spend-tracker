/**
 * The one place the client asks which locale it is in (#52, part of #48).
 *
 * Two questions, kept apart because #48 decided they are chosen separately,
 * per device:
 *
 * - **`uiLanguage()`** is the language the words are in. It is `"en"` until a
 *   translated catalog ships (#53 brings the catalogs, #58 the languages), so
 *   every word a screen builds -- a month's name, a country's name, a plural --
 *   stays the English it was.
 * - **`formatLocale()`** is how numbers, money and dates are written. It is the
 *   browser's own, as it always was: every `Intl` call that used to pass
 *   `undefined` now passes this, which resolves to the same locale.
 *
 * So nothing here changes what an English reader sees. What it changes is that
 * the next step -- a picker, a catalog -- has one seam to plug into rather
 * than a hundred call sites.
 *
 * The formatters are cached: an `Intl` object is expensive to build and the
 * register formats an amount per row. One cache per kind, keyed on the locale
 * and the options.
 */

// The Lingui singleton itself, not `./i18n`: that module imports the English
// catalog, and this one has to load anywhere `money.ts` does -- including the
// server's agreement test, which runs it under plain Node.
import { i18n } from "@lingui/core";

/** What the words are in until a catalog is served. */
export const SOURCE_LANGUAGE = "en";

let formatOverride: string | undefined;

/** The browser's own formatting locale, as `Intl` resolves `undefined`. */
function browserLocale(): string {
  try {
    return new Intl.NumberFormat().resolvedOptions().locale;
  } catch {
    return "en-US";
  }
}

/**
 * The language the words are in: the active Lingui catalog's (`lib/i18n.ts`).
 * English until #58 ships another; `en-XA` under the pseudo-locale.
 */
export function uiLanguage(): string {
  return i18n.locale || SOURCE_LANGUAGE;
}

/** Whether the words are English, so a screen keeps the English it had. */
export function speaksEnglish(): boolean {
  return uiLanguage().split("-")[0] === "en";
}

/** How numbers, money and dates are written: the browser's, unless pinned. */
export function formatLocale(): string {
  return formatOverride ?? browserLocale();
}

/**
 * The `lang` an amount carries: the formatting locale, when its language is
 * not the one the words are in (#271). "1 234,56 kr" inside a Portuguese
 * screen is Swedish, and a screen reader should read it so. `undefined` when
 * the two agree -- an English screen formatted in English carries nothing,
 * as before.
 */
export function amountLang(): string | undefined {
  const format = formatLocale();
  return format.split("-")[0].toLowerCase() === uiLanguage().split("-")[0].toLowerCase() ? undefined : format;
}

/**
 * Pin the formatting locale, or `undefined` to follow the browser again.
 *
 * For the tests, which must not depend on the machine that runs them, and for
 * the format picker in #53. Clears the caches, which hold formatters built for
 * the old one.
 */
export function setFormatLocale(locale: string | undefined): void {
  formatOverride = locale;
  for (const cache of CACHES) cache.clear();
}

const numberFormats = new Map<string, Intl.NumberFormat>();
const dateTimeFormats = new Map<string, Intl.DateTimeFormat>();
const collators = new Map<string, Intl.Collator>();
const pluralRulesCache = new Map<string, Intl.PluralRules>();
const displayNamesCache = new Map<string, Intl.DisplayNames>();
const CACHES: Map<string, unknown>[] = [
  numberFormats,
  dateTimeFormats,
  collators,
  pluralRulesCache,
  displayNamesCache,
];

function cached<T>(cache: Map<string, T>, locale: string, options: object | undefined, make: () => T): T {
  const key = `${locale}|${JSON.stringify(options ?? {})}`;
  let found = cache.get(key);
  if (found === undefined) {
    found = make();
    cache.set(key, found);
  }
  return found;
}

export function numberFormat(
  options?: Intl.NumberFormatOptions,
  locale: string = formatLocale(),
): Intl.NumberFormat {
  return cached(numberFormats, locale, options, () => new Intl.NumberFormat(locale, options));
}

export function dateTimeFormat(
  options?: Intl.DateTimeFormatOptions,
  locale: string = formatLocale(),
): Intl.DateTimeFormat {
  return cached(dateTimeFormats, locale, options, () => new Intl.DateTimeFormat(locale, options));
}

/**
 * How names sort. The formatting locale, because that is what
 * `localeCompare` with no locale used: a Swedish browser already put Å after
 * Z, and still does.
 */
export function collator(options?: Intl.CollatorOptions, locale: string = formatLocale()): Intl.Collator {
  return cached(collators, locale, options, () => new Intl.Collator(locale, options));
}

/** The plural category of a count, in the language the words are in. */
export function pluralRules(
  options?: Intl.PluralRulesOptions,
  locale: string = uiLanguage(),
): Intl.PluralRules {
  return cached(pluralRulesCache, locale, options, () => new Intl.PluralRules(locale, options));
}

/** Names of regions, languages and currencies, in the language the words are in. */
export function displayNames(
  type: Intl.DisplayNamesOptions["type"],
  locale: string = uiLanguage(),
): Intl.DisplayNames | null {
  try {
    return cached(displayNamesCache, locale, { type }, () => new Intl.DisplayNames(locale, { type }));
  } catch {
    return null;
  }
}

/** Two people's, payees', accounts' or categories' names, in reading order. */
export function compareNames(a: string, b: string): number {
  return collator().compare(a, b);
}

/** A count, grouped the reader's way: what `n.toLocaleString()` wrote. */
export function formatCount(n: number): string {
  return numberFormat().format(n);
}

/** The character this locale marks decimals with. */
export function decimalSeparator(locale: string = formatLocale()): string {
  return numberFormat(undefined, locale).formatToParts(1.5).find((p) => p.type === "decimal")?.value ?? ".";
}

/**
 * A figure with a fixed number of decimals and no grouping: what
 * `value.toFixed(digits)` wrote, with the reader's decimal mark.
 *
 * Built on `toFixed` rather than `Intl` on purpose. The two round some
 * halves differently, and an exchange rate or a file size that moved in its
 * last digit for an English reader would be a visible change.
 */
export function formatFixed(value: number, digits: number): string {
  const text = value.toFixed(digits);
  const mark = decimalSeparator();
  return mark === "." ? text : text.replace(".", mark);
}

/**
 * A calendar date the server sent as `YYYY-MM-DD`, as the reader sees it.
 *
 * ISO today, for everybody: that is what every screen shows and what
 * `Household.date_format` defaults to. This is the seam the date-format
 * picker plugs into (#53), so a screen showing a date calls this rather than
 * printing the string.
 */
export function formatDate(iso: string): string {
  return iso;
}

const ENGLISH_MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * `2026-03` as `Mar 2026`, without constructing a local Date and risking the
 * shift to the month before.
 *
 * In English, the table the two reports each kept -- `Intl` in `en-GB` says
 * "Sept", and that would be a visible change. In any other language, `Intl`
 * reading the month as a UTC date.
 */
export function monthLabel(period: string): string {
  const [year, month] = period.split("-");
  const index = Number(month) - 1;
  if (speaksEnglish() || !(index >= 0 && index < 12)) {
    return `${ENGLISH_MONTHS[index] ?? month} ${year}`;
  }
  return dateTimeFormat({ month: "short", year: "numeric", timeZone: "UTC" }, uiLanguage()).format(
    Date.UTC(Number(year), index, 1),
  );
}

const ENGLISH_MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/**
 * A month's full name, from 0 for January. The English table in English, as
 * the date range's pickers had it; `Intl` in another language.
 */
export function monthName(index: number): string {
  if (speaksEnglish()) return ENGLISH_MONTH_NAMES[index] ?? String(index + 1);
  return dateTimeFormat({ month: "long", timeZone: "UTC" }, uiLanguage()).format(Date.UTC(2026, index, 1));
}

/**
 * Names joined into one phrase: "Casa and Flat 2". In English, joined with
 * " and " as the screens always did -- `Intl.ListFormat` would put a comma
 * before the last of three, which is a visible change. In another language,
 * `Intl.ListFormat`, which knows its own conjunction.
 */
export function listText(items: readonly string[]): string {
  if (speaksEnglish()) return items.join(" and ");
  try {
    return new Intl.ListFormat(uiLanguage(), { type: "conjunction" }).format(items);
  } catch {
    return items.join(", ");
  }
}

/**
 * "EUR", "EUR or GBP", "EUR, GBP or USD" -- the alternatives a sentence offers.
 * English keeps the form it always had, without the serial comma; any other
 * language gets its own disjunction from Intl.
 */
export function orText(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  if (speaksEnglish()) return `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;
  try {
    return new Intl.ListFormat(uiLanguage(), { type: "disjunction" }).format(items);
  } catch {
    return items.join(", ");
  }
}

/**
 * A country's name. In English, the name the server sent, which is the list
 * the account form searches; in another language, `Intl.DisplayNames`.
 */
export function countryName(code: string, english: string): string {
  if (speaksEnglish()) return english;
  return displayNames("region")?.of(code.toUpperCase()) ?? english;
}
