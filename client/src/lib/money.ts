/**
 * Money is an integer of minor units, never a float.
 *
 * This mirrors app/money.py. The previous build had the same duplication with
 * nothing checking the two agreed; a Python test now parses EXPONENTS below and
 * asserts it matches, so drift fails CI rather than a report.
 */

import { formatLocale, numberFormat } from "./locale";

// ISO 4217 exponents that are not 2. Everything unlisted uses 2.
export const EXPONENTS: Record<string, number> = {
  JPY: 0, KRW: 0, CLP: 0, ISK: 0, VND: 0, HUF: 0, TWD: 0,
  BHD: 3, IQD: 3, JOD: 3, KWD: 3, LYD: 3, OMR: 3, TND: 3,
};

export function exponent(currency: string): number {
  return EXPONENTS[currency.toUpperCase()] ?? 2;
}

export function minorFactor(currency: string): number {
  return 10 ** exponent(currency);
}

/**
 * Minor units as a plain decimal string -- `-123456` in EUR is `"-1234.56"` --
 * built from the digits, never by dividing. `Intl.NumberFormat` reads a string
 * as an exact decimal, so an amount past 2^53 / 100 is still written as the
 * figure that was stored rather than the nearest float to it.
 */
export function decimalText(minor: number, currency: string): string {
  const digits = exponent(currency);
  const magnitude = String(Math.abs(Math.trunc(minor))).padStart(digits + 1, "0");
  const whole = magnitude.slice(0, magnitude.length - digits);
  const fraction = digits ? `.${magnitude.slice(magnitude.length - digits)}` : "";
  return `${minor < 0 ? "-" : ""}${whole}${fraction}`;
}

/**
 * `Intl` given a decimal string. The types say `number`; every engine this app
 * supports takes the string as an exact decimal (Intl.NumberFormat v3), and one
 * that did not would read it as the same number the division used to give.
 */
function formatDecimal(formatter: Intl.NumberFormat, text: string): string {
  return formatter.format(text as unknown as number);
}

/**
 * Render minor units for a human, in the account's own currency.
 *
 * The currency's decimals are this file's table, not ICU's, and are passed as
 * both the minimum and the maximum: ICU and ISO disagree about a few (ISK,
 * and the cash rounding some currencies use), and the ledger stores ISO's.
 * The locale is the reader's formatting locale (`lib/locale.ts`), which is the
 * browser's own, as `undefined` was.
 */
export function format(minor: number, currency: string): string {
  const digits = exponent(currency);
  const text = decimalText(minor, currency);
  // `Intl` throws a RangeError for a code that is not three letters ("€€€",
  // "12A"), and this runs per row on every screen: one bad code stored in the
  // ledger blanked the whole household (#193). The server refuses them now,
  // but a row written before that still has to render, so it renders as text.
  if (/^[A-Za-z]{3}$/.test(currency)) {
    try {
      return formatDecimal(
        numberFormat({
          style: "currency",
          currency: currency.toUpperCase(),
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        }),
        text,
      );
    } catch {
      // fall through to the plain rendering
    }
  }
  const plain = formatDecimal(
    numberFormat({ minimumFractionDigits: digits, maximumFractionDigits: digits }),
    text.replace(/^-/, ""),
  );
  return `${minor < 0 ? "-" : ""}${plain} ${currency}`;
}

/**
 * Render minor units back into a text box for editing.
 *
 * Not `format()` with the symbol stripped: `format` inserts a *grouping*
 * separator, and `parse` reads the last separator as the decimal point. For a
 * currency with no decimals there is nothing to disambiguate against, so
 * "1,234" came back as 1 -- a saved yen amount divided by a thousand. This
 * emits ungrouped digits with the currency's own number of decimals, so
 * parse(toInput(n)) === n for every currency.
 *
 * The decimal mark is the reader's own (`locale`, or the browser's), because
 * `parse` resolves a lone mark before three digits by that format: "1.234" in
 * a three-decimal currency is a thousand to a German reader, so a German
 * reader's box says "1,234".
 */
export function toInput(minor: number, currency: string, locale?: string): string {
  const figure = decimalText(Math.abs(minor), currency);
  const { decimal } = separators(locale);
  return decimal === "," ? figure.replace(".", ",") : figure;
}

/**
 * The characters this locale groups thousands and marks decimals with.
 *
 * `1234567.5` rather than `1234.5`: Spanish, among others, does not group a
 * four-digit number at all, and would report no group character.
 */
function separators(locale?: string): { group: string | null; decimal: string } {
  try {
    const parts = numberFormat(undefined, locale ?? formatLocale()).formatToParts(1234567.5);
    return {
      group: parts.find((p) => p.type === "group")?.value ?? null,
      decimal: parts.find((p) => p.type === "decimal")?.value ?? ".",
    };
  } catch {
    return { group: ",", decimal: "." };
  }
}

/**
 * Digits with at most one `.` as the decimal point, or null.
 *
 * Takes text that holds only digits, `,` and `.`. When both separators appear
 * the last one is the decimal mark -- "1.234,56" and "1,234.56" say so
 * themselves. When only one kind appears it is not that simple: "1,234" is a
 * thousand in English and one-and-a-bit in German, and reading it by position
 * alone stored 1.23 for a typed thousand (#45). So a lone separator followed
 * by exactly three digits is resolved by the reader's number format; one that
 * format does not use either way is refused rather than guessed. A separator
 * that repeats ("1,234,567") can only be grouping, and every group after the
 * first must then be three digits.
 */
function normalise(text: string, locale?: string): string | null {
  const lastComma = text.lastIndexOf(",");
  const lastDot = text.lastIndexOf(".");
  if (lastComma >= 0 && lastDot >= 0) {
    return lastComma > lastDot
      ? text.replace(/\./g, "").replace(",", ".")
      : text.replace(/,/g, "");
  }
  const sep = lastComma >= 0 ? "," : lastDot >= 0 ? "." : null;
  if (sep === null) return text;

  const pieces = text.split(sep);
  if (pieces.length > 2) {
    const [head, ...groups] = pieces;
    if (!/^\d{1,3}$/.test(head) || !groups.every((g) => /^\d{3}$/.test(g))) return null;
    return pieces.join("");
  }
  const [whole, frac] = pieces;
  // "1234.567" cannot be grouping -- a first group is at most three digits.
  const ambiguous = /^\d{1,3}$/.test(whole) && frac.length === 3;
  if (!ambiguous) return `${whole}.${frac}`;

  const { group, decimal } = separators(locale);
  if (sep === decimal) return `${whole}.${frac}`;
  if (sep === group) return `${whole}${frac}`;
  return null;
}

/**
 * What someone typed, with the characters other locales write folded into the
 * ones `parse` reads, and every space gone.
 *
 * - Spaces of every width: U+00A0 and U+202F are what sv-SE and fr-FR group
 *   thousands with, U+2009 what some style guides do. Copying an amount out of
 *   this app in Swedish pastes a U+00A0.
 * - Apostrophes, straight or curly: de-CH groups with them ("1'234.50").
 * - U+2212 MINUS SIGN and the dashes beside it: sv-SE writes a negative with
 *   U+2212, and a minus that was dropped would turn money out into money in.
 * - Parentheses are kept: `parse` reads them as accounting's negative.
 */
function unify(text: string): string {
  return (text ?? "")
    .replace(/[\u2212\u2012\u2013\u2014\uFE63\uFF0D]/g, "-")
    .replace(/[\s'\u2019\u02BC]/g, "");
}

/**
 * Read what someone typed.
 *
 * Tolerant on purpose: "12,34", "-1.234,56" and "(12.34)" all mean what they
 * look like. "1,234" and "1.234" mean what they mean in the reader's number
 * format -- `locale`, or the browser's when it is not given -- and nothing if
 * that format uses neither mark (see `normalise`). Returns null for anything
 * that is not a number, so a caller can tell "nothing yet" from "zero".
 */
export function parse(text: string, currency: string, locale?: string): number | null {
  let cleaned = unify(text);
  if (!cleaned) return null;

  let negative = false;
  if (cleaned.startsWith("(") && cleaned.endsWith(")")) {
    negative = true;
    cleaned = cleaned.slice(1, -1);
  }
  cleaned = cleaned.replace(/[^\d,.\-+]/g, "");
  if (!cleaned || cleaned === "-" || cleaned === "+") return null;

  if (cleaned.includes("-")) negative = !negative;
  const normalised = normalise(cleaned.replace(/[-+]/g, ""), locale);
  if (normalised === null) return null;
  cleaned = normalised;

  // Digits, not floats. `Math.round(8.165 * 100)` is 816 because 8.165 is not
  // 8.165 in binary, while app/money.py quantizes a Decimal ROUND_HALF_UP and
  // gets 817. CLAUDE.md says money is never a float anywhere; this is the one
  // place the client could have broken that, and a cent of disagreement is a
  // cent the two sides never reconcile.
  const parts = cleaned.split(".");
  if (parts.length > 2) return null;
  const [whole = "", frac = ""] = parts;
  if (!/^\d*$/.test(whole) || !/^\d*$/.test(frac)) return null;
  if (whole === "" && frac === "") return null;

  const digits = exponent(currency);
  const kept = frac.slice(0, digits).padEnd(digits, "0");
  // Half up on the magnitude, which is away from zero once the sign goes back
  // on -- the same direction ROUND_HALF_UP takes on the server.
  const roundUp = frac.length > digits && frac[digits] >= "5";

  const minor = Number(`${whole || "0"}${kept}`) + (roundUp ? 1 : 0);
  if (!Number.isSafeInteger(minor)) return null;
  return negative ? -minor : minor;
}

/**
 * What someone typed into the register's amount lookup, as the server reads it.
 *
 * The lookup searches every money column at once -- Out and In, in every
 * currency (#123) -- so this cannot turn the text into minor units the way
 * `parse` does: which unit depends on a currency nobody named. It only
 * normalises the text the same way `parse` reads it, and the server works out
 * what the figure means in each currency (`app/money.py`, `magnitude_span`).
 *
 * The sign is dropped, because Out and In are one column read two ways and
 * "45" means either. Returns an unsigned decimal with a `.` point --
 * "1.234,56" is "1234.56" -- or null for anything that is not an amount.
 */
export function amountLookup(text: string, locale?: string): string | null {
  let cleaned = unify(text).replace(/[()+-]/g, "");
  cleaned = cleaned.replace(/[^\d,.]/g, "");
  if (!cleaned) return null;

  const normalised = normalise(cleaned, locale);
  if (normalised === null) return null;
  cleaned = normalised;

  if (!/^\d*(\.\d*)?$/.test(cleaned)) return null;
  const [whole, frac] = cleaned.split(".");
  if (!whole && !frac) return null;
  // "45." is 45, whole; ".5" is 0.5.
  return frac ? `${whole || "0"}.${frac}` : whole || "0";
}
