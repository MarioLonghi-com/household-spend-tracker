/**
 * Every screen the shell can show, by the name `App.tsx` switches on.
 *
 * A list rather than only a type so that something other than the shell can
 * check a name against it: a help article's `screens:` and its
 * `[…](screen:<name>)` links (#460) are held to this list by
 * `src/help/articles.test.ts`, so a renamed screen fails there rather than
 * leaving an article that opens nothing.
 */

/** The report screens, one per report: `report-<key>`. */
export const REPORT_KEYS = ["income-expense", "reimbursements", "net-worth"] as const;

export const SCREENS = [
  "register",
  "accounts",
  "categories",
  "receipts",
  "reports",
  ...REPORT_KEYS.map((key) => `report-${key}` as const),
  "household",
  "exchange-rates",
  "payees",
  "payee-categorisation",
  "transfers",
  "import-guide",
  "import",
  "rules",
  "history",
  "admin",
  "application",
] as const;

export type Screen = (typeof SCREENS)[number];

export function isScreen(name: string): name is Screen {
  return (SCREENS as readonly string[]).includes(name);
}
