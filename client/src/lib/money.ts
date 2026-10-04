/**
 * Money is an integer of minor units, never a float.
 *
 * This mirrors app/money.py. The previous build had the same duplication with
 * nothing checking the two agreed; a Python test now parses EXPONENTS below and
 * asserts it matches, so drift fails CI rather than a report.
 */

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

/** Render minor units for a human, in the account's own currency. */
export function format(minor: number, currency: string): string {
  const digits = exponent(currency);
  const figure = minor / minorFactor(currency);
  // `Intl` throws a RangeError for a code that is not three letters ("€€€",
  // "12A"), and this runs per row on every screen: one bad code stored in the
  // ledger blanked the whole household (#193). The server refuses them now,
  // but a row written before that still has to render, so it renders as text.
  if (/^[A-Za-z]{3}$/.test(currency)) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: currency.toUpperCase(),
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }).format(figure);
    } catch {
      // fall through to the plain rendering
    }
  }
  const plain = Math.abs(figure).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
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
 */
export function toInput(minor: number, currency: string): string {
  const digits = exponent(currency);
  return (Math.abs(minor) / minorFactor(currency)).toFixed(digits);
}

/**
 * Read what someone typed.
 *
 * Tolerant on purpose: "12,34", "-1.234,56" and "(12.34)" all mean what they
 * look like. Returns null for anything that is not a number, so a caller can
 * tell "nothing yet" from "zero".
 */
export function parse(text: string, currency: string): number | null {
  let cleaned = (text ?? "").trim().replace(/[\s  ]/g, "");
  if (!cleaned) return null;

  let negative = false;
  if (cleaned.startsWith("(") && cleaned.endsWith(")")) {
    negative = true;
    cleaned = cleaned.slice(1, -1);
  }
  cleaned = cleaned.replace(/[^\d,.\-+]/g, "");
  if (!cleaned || cleaned === "-" || cleaned === "+") return null;

  const lastComma = cleaned.lastIndexOf(",");
  const lastDot = cleaned.lastIndexOf(".");
  if (lastComma > lastDot) {
    cleaned = cleaned.replace(/\./g, "").replace(",", ".");
  } else {
    cleaned = cleaned.replace(/,/g, "");
  }

  if (cleaned.includes("-")) negative = !negative;
  cleaned = cleaned.replace(/[-+]/g, "");

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
export function amountLookup(text: string): string | null {
  let cleaned = (text ?? "").trim().replace(/[\s  ()+-]/g, "");
  cleaned = cleaned.replace(/[^\d,.]/g, "");
  if (!cleaned) return null;

  const lastComma = cleaned.lastIndexOf(",");
  const lastDot = cleaned.lastIndexOf(".");
  cleaned =
    lastComma > lastDot
      ? cleaned.replace(/\./g, "").replace(",", ".")
      : cleaned.replace(/,/g, "");

  if (!/^\d*(\.\d*)?$/.test(cleaned)) return null;
  const [whole, frac] = cleaned.split(".");
  if (!whole && !frac) return null;
  // "45." is 45, whole; ".5" is 0.5.
  return frac ? `${whole || "0"}.${frac}` : whole || "0";
}
