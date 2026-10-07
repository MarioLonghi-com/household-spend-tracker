/**
 * The words for each refusal code the server sends (#53, codes from #65).
 *
 * The server answers a converted refusal with `detail` (its English
 * sentence), a `code` and raw `params`. This file is the client's half of
 * `app/error_codes.py`: one message per code, with the same id and the same
 * English template, so the catalogs carry them. `tests/test_error_codes.py`
 * fails when the two files disagree.
 *
 * **In English the screen still shows `detail`**, byte for byte; the
 * templates are written for translation and are not the sentence a person
 * reads today. A translated catalog uses the template, with the params
 * formatted for its locale: money from minor units and its currency, dates
 * through `formatDate`.
 */

import { msg } from "@lingui/core/macro";
import type { MessageDescriptor } from "@lingui/core";
import { ApiError } from "./api";
import { i18n, SOURCE_LOCALE } from "./i18n";
import { formatDate } from "./locale";
import { format } from "./money";

interface ErrorMessage {
  message: MessageDescriptor;
  /** Params that are minor units of the `currency` param. */
  money?: readonly string[];
  /** Params that are ISO calendar dates. */
  dates?: readonly string[];
}

export const ERROR_MESSAGES: Record<string, ErrorMessage> = {
  "currency.not_iso_4217": {
    message: msg({
      id: "error.currency.not_iso_4217",
      message: "{code} is not an ISO 4217 currency code. Check the spelling, like GBP or EUR",
    }),
  },
  "money.decimals_in_whole_currency": {
    message: msg({
      id: "error.money.decimals_in_whole_currency",
      message: "{value} has decimals, and {currency} has none",
    }),
  },
  "money.not_an_amount": {
    message: msg({
      id: "error.money.not_an_amount",
      message:
        "{value} is not an amount this can read. Write it with a point before the decimals and no thousands separators, like 1234.56 or -80",
    }),
  },
  "money.too_large": {
    message: msg({ id: "error.money.too_large", message: "{value} is too large to record as money" }),
  },
  "money.too_many_decimals": {
    message: msg({
      id: "error.money.too_many_decimals",
      message: "{value} has more decimals than {currency} has ({places})",
    }),
  },
  "money.too_many_digits": {
    message: msg({
      id: "error.money.too_many_digits",
      message: "An amount {digits} digits long is too large to record as money",
    }),
  },
  "receipt.no_such_copy": {
    message: msg({ id: "error.receipt.no_such_copy", message: "That receipt has no copy of that kind" }),
  },
  "reconcile.does_not_balance": {
    message: msg({
      id: "error.reconcile.does_not_balance",
      message:
        "That does not balance: {difference} out. Tick or untick rows until the difference is zero, or add the transaction the statement has and the register does not.",
    }),
    money: ["difference"],
  },
  "reconcile.row_after_statement": {
    message: msg({
      id: "error.reconcile.row_after_statement",
      message: "A row dated {row_date} is after the statement closes on {statement_date}, so it cannot be on it",
    }),
    dates: ["row_date", "statement_date"],
  },
  "split.does_not_add_up": {
    message: msg({
      id: "error.split.does_not_add_up",
      message:
        "The parts come to {total} and the transaction is {amount}. A split has to add up, or it moves the balance.",
    }),
    money: ["total", "amount"],
  },
  "transfer.needs_amount_arriving": {
    message: msg({
      id: "error.transfer.needs_amount_arriving",
      message: "A {from_currency} to {to_currency} transfer needs the amount that arrives; we never invent a rate",
    }),
  },
  "transfer.same_account": {
    message: msg({ id: "error.transfer.same_account", message: "An account cannot transfer to itself" }),
  },
};

/** The params as the active locale writes them. */
function shown(entry: ErrorMessage, params: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...params };
  const currency = typeof params.currency === "string" ? params.currency : null;
  for (const key of entry.money ?? []) {
    if (currency && typeof params[key] === "number") out[key] = format(params[key] as number, currency);
  }
  for (const key of entry.dates ?? []) {
    if (typeof params[key] === "string") out[key] = formatDate(params[key] as string);
  }
  return out;
}

/**
 * What a failed request says to the person.
 *
 * The server's own sentence, unless the words are not English and the
 * refusal carries a code this build knows -- then the catalog's message.
 * An unknown code, a missing catalog entry, or a refusal from before the
 * codes all fall back to the sentence, which is always there.
 */
export function problemText(error: unknown): string {
  const sentence = error instanceof Error ? error.message : String(error);
  if (i18n.locale === SOURCE_LOCALE || !(error instanceof ApiError)) return sentence;
  const code = error.body?.code;
  const entry = typeof code === "string" ? ERROR_MESSAGES[code] : undefined;
  if (!entry) return sentence;
  const params = error.body?.params;
  return i18n._(entry.message.id, shown(entry, params && typeof params === "object" ? (params as Record<string, unknown>) : {}), {
    message: entry.message.message,
  });
}
