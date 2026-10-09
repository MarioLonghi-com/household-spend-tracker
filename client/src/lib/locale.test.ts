import { afterEach, describe, expect, it } from "vitest";
import {
  ACCOUNT_TYPE_HEADINGS,
  ACCOUNT_TYPE_LABELS,
  IMPORT_OUTCOME_WORDS,
  ROLE_LABELS,
  YNAB_ACCOUNT_WORDS,
  YNAB_CATEGORY_WORDS,
  YNAB_STEP_LABELS,
  roleLabel,
} from "./labels";
import {
  compareNames,
  countryName,
  formatCount,
  formatDate,
  formatFixed,
  formatLocale,
  monthLabel,
  numberFormat,
  orText,
  setFormatLocale,
  uiLanguage,
} from "./locale";
import { decimalText, format, parse, toInput } from "./money";
import { bytes } from "./bytes";
import { formatInstant } from "./time";
import { sortRows } from "./sorting";

// test-setup.ts pins en-US before every file; a test that moves it puts it back.
afterEach(() => setFormatLocale("en-US"));

/** What `format` wrote before #52: a float divided out, and `Intl` with no locale. */
function legacyFormat(minor: number, currency: string, locale: string): string {
  const digits = { JPY: 0, BHD: 3, KWD: 3 }[currency] ?? 2;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(minor / 10 ** digits);
}

describe("nothing an English reader sees changes", () => {
  it("speaks English and follows the pinned formatting locale", () => {
    expect([uiLanguage(), formatLocale()]).toEqual(["en", "en-US"]);
  });

  it("follows the browser's locale when nothing pins it, as `undefined` did", () => {
    setFormatLocale(undefined);
    expect(formatLocale()).toBe(new Intl.NumberFormat().resolvedOptions().locale);
  });

  it.each(["en-US", "en-GB", "de-DE", "sv-SE"])("formats money exactly as before, in %s", (locale) => {
    setFormatLocale(locale);
    const amounts = [0, 1, -1, 99, 100, -4250, 123456, -123456, 100000000, 987654321012];
    for (const currency of ["EUR", "GBP", "BRL", "SEK", "JPY", "BHD", "KWD"]) {
      for (const minor of amounts) {
        expect(format(minor, currency)).toBe(legacyFormat(minor, currency, locale));
      }
    }
  });

  it("names every month as the two reports' table did, even where Intl says Sept", () => {
    const table = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    for (const locale of ["en-US", "en-GB", "sv-SE"]) {
      setFormatLocale(locale);
      table.forEach((name, i) => {
        expect(monthLabel(`2026-${String(i + 1).padStart(2, "0")}`)).toBe(`${name} 2026`);
      });
    }
    // Reimbursements' fallback for a month that is not one.
    expect(monthLabel("2026-13")).toBe("13 2026");
  });

  it("shows a calendar date as the ISO the screens printed", () => {
    expect(formatDate("2026-03-02")).toBe("2026-03-02");
  });

  it("writes a fixed figure as toFixed did, and counts as toLocaleString did", () => {
    for (const [value, digits] of [[1.23456, 4], [0.5, 0], [1234.5, 1], [2.675, 2]] as const) {
      expect(formatFixed(value, digits)).toBe(value.toFixed(digits));
    }
    expect(formatCount(1234567)).toBe((1234567).toLocaleString("en-US"));
    expect(bytes(1536)).toBe("1.5 KiB");
  });

  it("writes an instant as toLocaleString did", () => {
    const when = "2026-03-02T10:15:00";
    expect(formatInstant(when)).toBe(new Date(`${when}Z`).toLocaleString("en-US"));
  });

  it("keeps the server's English name for a country", () => {
    expect(countryName("es", "Spain")).toBe("Spain");
    expect(countryName("cz", "Czech Republic")).toBe("Czech Republic");
  });

  it("keeps every label word for word", () => {
    expect(
      Object.fromEntries(Object.entries(ACCOUNT_TYPE_LABELS).map(([k, v]) => [k, v.label])),
    ).toEqual({
      checking: "Current account",
      savings: "Savings",
      cash: "Cash",
      credit_card: "Credit card",
      other_asset: "Other asset",
      other_liability: "Other debt",
    });
    expect(ACCOUNT_TYPE_LABELS.credit_card.blurb).toBe(
      "Money you owe the card issuer. Spending makes the balance more negative; paying the bill is a transfer from the account that pays it.",
    );
    expect(ACCOUNT_TYPE_HEADINGS).toEqual({
      checking: "Current accounts",
      savings: "Savings",
      cash: "Cash",
      credit_card: "Credit cards",
      other_asset: "Other assets",
      other_liability: "Other debts",
    });
    expect(IMPORT_OUTCOME_WORDS).toEqual({
      created: "New",
      matched_existing: "Already have it",
      duplicate_skipped: "Already imported",
      rejected: "Could not read",
      needs_review: "Needs a look",
      skipped: "Not for this account",
    });
    expect(Object.values(YNAB_STEP_LABELS)).toEqual([
      "Source app", "Connect", "Plan", "Review", "Accounts", "Categories", "Flags & options",
      "Preview", "Report",
    ]);
    expect([YNAB_ACCOUNT_WORDS.unmatched, YNAB_CATEGORY_WORDS.unmatched]).toEqual([
      "Skipped",
      "Uncategorised",
    ]);
    expect([ROLE_LABELS.owner, roleLabel("member"), roleLabel("guest")]).toEqual([
      "owner",
      "member",
      "guest",
    ]);
  });
});

describe("money in other locales", () => {
  it("writes the three first locales' money their own way", () => {
    setFormatLocale("pt-BR");
    expect(format(123456, "BRL")).toBe("R$ 1.234,56");
    setFormatLocale("es-ES");
    expect([format(123456, "EUR"), format(1234567, "EUR")]).toEqual([
      "1234,56 €",
      "12.345,67 €",
    ]);
    setFormatLocale("sv-SE");
    expect(format(-123456, "SEK")).toBe("−1 234,56 kr");
  });

  it("formats BRL, JPY and BHD with their own decimals", () => {
    expect([format(123456, "BRL"), format(123456, "JPY"), format(123456, "BHD")]).toEqual([
      "R$1,234.56",
      "¥123,456",
      "BHD 123.456",
    ]);
  });

  it("writes an amount past a float's precision as the digits that were stored", () => {
    expect(decimalText(Number.MAX_SAFE_INTEGER, "EUR")).toBe("90071992547409.91");
    expect(decimalText(-5, "BHD")).toBe("-0.005");
    expect(decimalText(7, "JPY")).toBe("7");
  });

  it.each(["en-US", "pt-BR", "es-ES", "sv-SE", "de-CH"])(
    "reads back what it wrote, in %s",
    (locale) => {
      setFormatLocale(locale);
      for (const [minor, currency] of [
        [123456, "BRL"],
        [-123456, "BRL"],
        [1234567, "EUR"],
        [-987654, "SEK"],
        [123456, "JPY"],
        [123456, "BHD"],
      ] as const) {
        expect(parse(format(minor, currency), currency, locale)).toBe(minor);
        expect(parse(toInput(minor, currency, locale), currency, locale)).toBe(Math.abs(minor));
      }
    },
  );

  it("reads the separators and minus signs other locales write", () => {
    expect(parse("1 234,56", "SEK", "sv-SE")).toBe(123456);
    expect(parse("1 234,56", "EUR", "fr-FR")).toBe(123456);
    expect(parse("1 234.56", "GBP", "en-GB")).toBe(123456);
    expect(parse("1'234.50", "CHF", "de-CH")).toBe(123450);
    expect(parse("1’234.50", "CHF", "de-CH")).toBe(123450);
    expect(parse("−45,00", "SEK", "sv-SE")).toBe(-4500);
    expect(parse("(45.00)", "GBP", "en-GB")).toBe(-4500);
  });

  it("reads 1,234 the reader's way: a thousand in English, one and a bit in German", () => {
    expect([parse("1,234", "EUR", "en"), parse("1,234", "EUR", "de")]).toEqual([123400, 123]);
  });

  it("caches one formatter per locale and options", () => {
    expect(numberFormat({ style: "percent" })).toBe(numberFormat({ style: "percent" }));
    setFormatLocale("de-DE");
    expect(numberFormat({ style: "percent" }).resolvedOptions().locale).toBe("de-DE");
  });
});

describe("names sort in reading order", () => {
  const names = ["Zoë", "Émile", "Ana", "Ángel", "eva", "Åsa", "Örjan"];

  it("puts accented names beside their letter in English", () => {
    expect([...names].sort(compareNames)).toEqual(["Ana", "Ángel", "Åsa", "Émile", "eva", "Örjan", "Zoë"]);
  });

  it("puts Å and Ö after Z in Swedish, as localeCompare did in a Swedish browser", () => {
    setFormatLocale("sv-SE");
    expect([...names].sort(compareNames)).toEqual(["Ana", "Ángel", "Émile", "eva", "Zoë", "Åsa", "Örjan"]);
    expect([...names].sort(compareNames)).toEqual(
      [...names].sort((a, b) => a.localeCompare(b, "sv-SE")),
    );
  });

  it("sorts a table column through the same collator", () => {
    const rows = names.map((name) => ({ name }));
    expect(sortRows(rows, "name", "asc", (row) => row.name).map((row) => row.name)).toEqual([
      "Ana", "Ángel", "Åsa", "Émile", "eva", "Örjan", "Zoë",
    ]);
  });
});

describe("words in another language, once one is served", () => {
  it("writes a fixed figure with the reader's decimal mark", () => {
    setFormatLocale("de-DE");
    expect(formatFixed(1.5, 1)).toBe("1,5");
    expect(bytes(1536)).toBe("1,5 KiB");
  });
});

describe("alternatives in a sentence", () => {
  it("keeps the English it had: no serial comma, and one is just itself", () => {
    expect(orText([])).toBe("");
    expect(orText(["EUR"])).toBe("EUR");
    expect(orText(["EUR", "GBP"])).toBe("EUR or GBP");
    expect(orText(["EUR", "GBP", "USD"])).toBe("EUR, GBP or USD");
  });

  it("asks Intl for the disjunction in another language", async () => {
    const { i18n } = await import("@lingui/core");
    const was = i18n.locale;
    i18n.load("es-ES", {});
    i18n.activate("es-ES");
    try {
      expect(orText(["EUR", "GBP", "USD"])).toBe("EUR, GBP o USD");
    } finally {
      i18n.activate(was);
    }
  });
});
