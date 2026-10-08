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
  "backup.protected": {
    message: msg({
      id: "error.backup.protected",
      message:
        "{name} is one of the newest five update backups. They are kept so an update can be undone, and the updater removes older ones itself",
    }),
  },
  "category.archived_default": {
    message: msg({ id: "error.category.archived_default", message: "{name} is archived, so it cannot be a default" }),
  },
  "category.choose_default": {
    message: msg({ id: "error.category.choose_default", message: "Choose the category to always use" }),
  },
  "category.group_name_taken": {
    message: msg({ id: "error.category.group_name_taken", message: "There is already a group called {name}" }),
  },
  "category.group_needs_name": {
    message: msg({ id: "error.category.group_needs_name", message: "A group needs a name" }),
  },
  "category.group_not_empty": {
    message: msg({
      id: "error.category.group_not_empty",
      message:
        "A group can only be deleted when there are no categories under it. {name} still holds {held, plural, one {# category} other {# categories}}{archived, plural, =0 {} other {, # of them archived}}. Move or delete them first.",
    }),
  },
  "category.group_not_found": {
    message: msg({ id: "error.category.group_not_found", message: "No such category group" }),
  },
  "category.in_use": {
    message: msg({
      id: "error.category.in_use",
      message:
        "{count, plural, one {# transaction is} other {# transactions are}} categorised as {name}. Archive it instead, and they keep their category.",
    }),
  },
  "category.name_taken": {
    message: msg({ id: "error.category.name_taken", message: "There is already a category called {name}" }),
  },
  "category.needs_name": {
    message: msg({ id: "error.category.needs_name", message: "A category needs a name" }),
  },
  "category.not_found": {
    message: msg({ id: "error.category.not_found", message: "No such category" }),
  },
  "category.other_household": {
    message: msg({ id: "error.category.other_household", message: "That category belongs to a different household" }),
  },
  "currency.not_iso_4217": {
    message: msg({
      id: "error.currency.not_iso_4217",
      message: "{code} is not an ISO 4217 currency code. Check the spelling, like GBP or EUR",
    }),
  },
  "money.amount_too_large": {
    message: msg({ id: "error.money.amount_too_large", message: "That amount is too large to record as money" }),
  },
  "money.decimals_in_whole_currency": {
    message: msg({
      id: "error.money.decimals_in_whole_currency",
      message: "{value} has decimals, and {currency} has none",
    }),
  },
  "money.not_a_value": {
    message: msg({ id: "error.money.not_a_value", message: "{value} is not a monetary value" }),
  },
  "money.not_an_amount": {
    message: msg({
      id: "error.money.not_an_amount",
      message:
        "{value} is not an amount this can read. Write it with a point before the decimals and no thousands separators, like 1234.56 or -80",
    }),
  },
  "money.not_whole_minor_units": {
    message: msg({
      id: "error.money.not_whole_minor_units",
      message:
        "{milliunits} thousandths is not a whole number of {currency} minor units",
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
  "payee.merge_different_households": {
    message: msg({
      id: "error.payee.merge_different_households",
      message:
        "Those payees are in different households",
    }),
  },
  "payee.merge_into_itself": {
    message: msg({ id: "error.payee.merge_into_itself", message: "A payee cannot be merged into itself" }),
  },
  "payee.needs_name": {
    message: msg({ id: "error.payee.needs_name", message: "A payee needs a name" }),
  },
  "payee.other_household": {
    message: msg({ id: "error.payee.other_household", message: "That payee belongs to a different household" }),
  },
  "payee.rule_needs_pattern": {
    message: msg({ id: "error.payee.rule_needs_pattern", message: "A rule needs something to match on" }),
  },
  "payee.rule_needs_payee": {
    message: msg({
      id: "error.payee.rule_needs_payee",
      message:
        "A rule that names a payee needs a payee to point at",
    }),
  },
  "payee.rule_not_a_regex": {
    message: msg({ id: "error.payee.rule_not_a_regex", message: "That is not a valid regular expression: {reason}" }),
  },
  "payee.rule_pattern_too_long": {
    message: msg({
      id: "error.payee.rule_pattern_too_long",
      message:
        "That pattern is too long (max {max} characters)",
    }),
  },
  "payee.rule_replacement_on_map": {
    message: msg({
      id: "error.payee.rule_replacement_on_map",
      message:
        "A replacement only means something on a rule that rewrites; this one names a payee",
    }),
  },
  "payee.rule_replacement_too_long": {
    message: msg({
      id: "error.payee.rule_replacement_too_long",
      message:
        "That replacement is too long (max {max} characters)",
    }),
  },
  "payee.rule_template_no_groups": {
    message: msg({
      id: "error.payee.rule_template_no_groups",
      message:
        "That replacement uses {reference}, and the pattern has no bracketed groups. Put brackets round the part you want to keep.",
    }),
  },
  "payee.rule_template_no_such_group": {
    message: msg({
      id: "error.payee.rule_template_no_such_group",
      message:
        "That replacement uses {reference}, and the pattern has no group called {name}",
    }),
  },
  "payee.rule_template_too_few_groups": {
    message: msg({
      id: "error.payee.rule_template_too_few_groups",
      message:
        "That replacement uses {reference}, and the pattern has only {groups} bracketed groups",
    }),
  },
  "profile.current_factor_refused": {
    message: msg({
      id: "error.profile.current_factor_refused",
      message:
        "That is not a working code from your current authenticator, or an unused recovery code",
    }),
  },
  "profile.needs_authenticator": {
    message: msg({
      id: "error.profile.needs_authenticator",
      message:
        "Set up an authenticator before making new recovery codes",
    }),
  },
  "profile.new_code_wrong": {
    message: msg({
      id: "error.profile.new_code_wrong",
      message:
        "That code is not right. Check the time on your phone and try again.",
    }),
  },
  "profile.recovery_grant_not_needed": {
    message: msg({
      id: "error.profile.recovery_grant_not_needed",
      message:
        "Your current authenticator can be checked again, so prove it with a code from it (or an unused recovery code) rather than the permission from signing in.",
    }),
  },
  "profile.recovery_grant_refused": {
    message: msg({
      id: "error.profile.recovery_grant_refused",
      message:
        "That permission to set up a new authenticator has expired, or is not for this session. Use an unused recovery code in its place.",
    }),
  },
  "profile.reenrolment_expired": {
    message: msg({ id: "error.profile.reenrolment_expired", message: "That re-enrolment has expired; start again" }),
  },
  "profile.reenrolment_not_yours": {
    message: msg({ id: "error.profile.reenrolment_not_yours", message: "That re-enrolment belongs to somebody else" }),
  },
  "profile.regenerate_refused": {
    message: msg({
      id: "error.profile.regenerate_refused",
      message:
        "That password and authenticator code do not prove it is you",
    }),
  },
  "profile.same_password": {
    message: msg({ id: "error.profile.same_password", message: "That is already your password" }),
  },
  "profile.wrong_password": {
    message: msg({ id: "error.profile.wrong_password", message: "That is not your current password" }),
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
  "reimbursement.not_a_work_expense_cannot_be_paid": {
    message: msg({
      id: "error.reimbursement.not_a_work_expense_cannot_be_paid",
      message:
        "A row that is not a work expense cannot have been paid back by anything",
    }),
  },
  "reimbursement.not_money_in": {
    message: msg({
      id: "error.reimbursement.not_money_in",
      message:
        "A reimbursement is money arriving, so pick money coming in",
    }),
  },
  "reimbursement.not_money_out": {
    message: msg({
      id: "error.reimbursement.not_money_out",
      message:
        "Only money leaving an account can be a work expense; this row is money coming in",
    }),
  },
  "reimbursement.paid_cannot_be_written_off": {
    message: msg({
      id: "error.reimbursement.paid_cannot_be_written_off",
      message:
        "This has been paid back. Take the payment off it before writing it off.",
    }),
  },
  "reimbursement.payment_cannot_be_expense": {
    message: msg({
      id: "error.reimbursement.payment_cannot_be_expense",
      message:
        "This row paid work expenses back, so it cannot be one itself",
    }),
  },
  "reimbursement.reimburses_itself": {
    message: msg({ id: "error.reimbursement.reimburses_itself", message: "A transaction cannot reimburse itself" }),
  },
  "reimbursement.settlement_is_work_expense": {
    message: msg({
      id: "error.reimbursement.settlement_is_work_expense",
      message:
        "That row is itself a work expense, so it cannot also be what paid one back",
    }),
  },
  "reimbursement.sign_change_on_payment": {
    message: msg({
      id: "error.reimbursement.sign_change_on_payment",
      message:
        "This row paid work expenses back, so it has to stay money coming in. Take the expenses off it first if the amount really changed direction.",
    }),
  },
  "reimbursement.sign_change_on_work_expense": {
    message: msg({
      id: "error.reimbursement.sign_change_on_work_expense",
      message:
        "This is a work expense, so it has to stay money going out. Set it to not a work expense first if the amount really changed direction.",
    }),
  },
  "reimbursement.transfer_is_not_a_reimbursement": {
    message: msg({
      id: "error.reimbursement.transfer_is_not_a_reimbursement",
      message:
        "A transfer between your own accounts is not a reimbursement",
    }),
  },
  "reimbursement.transfer_is_not_a_work_expense": {
    message: msg({
      id: "error.reimbursement.transfer_is_not_a_work_expense",
      message:
        "This is one leg of a transfer, money moving between your own accounts. Flag the purchase it paid for instead.",
    }),
  },
  "reimbursement.written_off_cannot_be_paid": {
    message: msg({
      id: "error.reimbursement.written_off_cannot_be_paid",
      message:
        "This was written off. Set it back to expected before recording a payment for it.",
    }),
  },
  "split.does_not_add_up": {
    message: msg({
      id: "error.split.does_not_add_up",
      message:
        "The parts come to {total} and the transaction is {amount}. A split has to add up, or it moves the balance.",
    }),
    money: ["total", "amount"],
  },
  "split.part_count": {
    message: msg({ id: "error.split.part_count", message: "A split is between {min} and {max} parts" }),
  },
  "split.repaid_work_expenses": {
    message: msg({
      id: "error.split.repaid_work_expenses",
      message:
        "This payment repaid work expenses. Take them off it before splitting it, then link each one to the part that paid it.",
    }),
  },
  "split.transfer_leg": {
    message: msg({
      id: "error.split.transfer_leg",
      message:
        "This is one leg of a transfer, which is a single movement of money recorded twice. Split the other side of the transfer too, or undo it first.",
    }),
  },
  "split.work_expense_money_in": {
    message: msg({
      id: "error.split.work_expense_money_in",
      message:
        "Every part of a work expense has to be money going out, because each part stays a work expense",
    }),
  },
  "split.zero_part": {
    message: msg({ id: "error.split.zero_part", message: "A part of a split cannot be zero" }),
  },
  "transaction.import_line_exists": {
    message: msg({
      id: "error.transaction.import_line_exists",
      message:
        "That statement line is already in this account",
    }),
  },
  "transaction.locked": {
    message: msg({
      id: "error.transaction.locked",
      message:
        "That transaction is locked; set it back to cleared before editing it",
    }),
  },
  "transaction.not_found": {
    message: msg({ id: "error.transaction.not_found", message: "No such transaction" }),
  },
  "transfer.amount_not_positive": {
    message: msg({ id: "error.transfer.amount_not_positive", message: "A transfer amount must be positive" }),
  },
  "transfer.arriving_not_positive": {
    message: msg({ id: "error.transfer.arriving_not_positive", message: "The amount arriving must be positive" }),
  },
  "transfer.different_households": {
    message: msg({ id: "error.transfer.different_households", message: "Those accounts are in different households" }),
  },
  "transfer.duplicate_leg": {
    message: msg({
      id: "error.transfer.duplicate_leg",
      message:
        "Make a new transfer from the register, not a copy of one leg of this one",
    }),
  },
  "transfer.edit_cross_currency_leg": {
    message: msg({
      id: "error.transfer.edit_cross_currency_leg",
      message:
        "Edit each leg of a cross-currency transfer on its own; we will not re-derive a rate",
    }),
  },
  "transfer.has_no_category": {
    message: msg({ id: "error.transfer.has_no_category", message: "A transfer has no category" }),
  },
  "transfer.link_already_a_transfer": {
    message: msg({
      id: "error.transfer.link_already_a_transfer",
      message:
        "One of those is already a transfer; unlink it first",
    }),
  },
  "transfer.link_no_money": {
    message: msg({
      id: "error.transfer.link_no_money",
      message:
        "A row with no money in it cannot be a transfer leg",
    }),
  },
  "transfer.link_repayment": {
    message: msg({
      id: "error.transfer.link_repayment",
      message:
        "One of those is the payment that repaid a work expense, not a transfer. Take it off the expenses it repaid first if it really is a transfer.",
    }),
  },
  "transfer.link_same_account": {
    message: msg({
      id: "error.transfer.link_same_account",
      message:
        "Both of those are in the same account; a transfer moves between two",
    }),
  },
  "transfer.link_split_part": {
    message: msg({ id: "error.transfer.link_split_part", message: "A part of a split cannot be a transfer leg" }),
  },
  "transfer.link_work_expense": {
    message: msg({
      id: "error.transfer.link_work_expense",
      message:
        "One of those is a work expense, which is money spent, not moved. Take the work-expense flag off it first if it really is a transfer.",
    }),
  },
  "transfer.needs_amount_arriving": {
    message: msg({
      id: "error.transfer.needs_amount_arriving",
      message: "A {from_currency} to {to_currency} transfer needs the amount that arrives; we never invent a rate",
    }),
  },
  "transfer.not_a_transfer": {
    message: msg({ id: "error.transfer.not_a_transfer", message: "That transaction is not a transfer" }),
  },
  "transfer.pair_in_two_households": {
    message: msg({
      id: "error.transfer.pair_in_two_households",
      message:
        "Those transactions are in different households",
    }),
  },
  "transfer.pair_same_direction": {
    message: msg({
      id: "error.transfer.pair_same_direction",
      message:
        "A transfer takes money out of one account and into the other",
    }),
  },
  "transfer.pair_with_itself": {
    message: msg({ id: "error.transfer.pair_with_itself", message: "A transaction cannot be a transfer with itself" }),
  },
  "transfer.same_account": {
    message: msg({ id: "error.transfer.same_account", message: "An account cannot transfer to itself" }),
  },
  "transfer.sides_differ": {
    message: msg({
      id: "error.transfer.sides_differ",
      message:
        "Both sides of a same-currency transfer must be the same amount",
    }),
  },
  "update.digest_mismatch": {
    message: msg({
      id: "error.update.digest_mismatch",
      message:
        "The image digests are not the ones the prepare report verified. Prepare the update again",
    }),
  },
  "update.engine_refused": {
    message: msg({
      id: "error.update.engine_refused",
      message:
        "The updater cannot use the container engine ({socket}), so it cannot update anything",
    }),
  },
  "update.in_flight": {
    message: msg({
      id: "error.update.in_flight",
      message:
        "Another update request is waiting or running. Wait for it to finish, then try again",
    }),
  },
  "update.lossy_mismatch": {
    message: msg({
      id: "error.update.lossy_mismatch",
      message:
        "Tick every migration that cannot be undone, and only those: the update needs exactly the ones the report lists",
    }),
  },
  "update.no_report": {
    message: msg({
      id: "error.update.no_report",
      message:
        "There is no prepared update with that id for this version. Check for updates and prepare it again",
    }),
  },
  "update.no_updater": {
    message: msg({
      id: "error.update.no_updater",
      message:
        "Updating from this screen needs the updater, and none has answered in the last two minutes",
    }),
  },
  "update.not_a_version": {
    message: msg({
      id: "error.update.not_a_version",
      message:
        "{version} is not a release version like 1.2.3",
    }),
  },
  "update.not_newer": {
    message: msg({
      id: "error.update.not_newer",
      message:
        "{to_version} is not newer than {running}, which this instance runs. An update never goes back",
    }),
  },
  "update.outcome_not_found": {
    message: msg({
      id: "error.update.outcome_not_found",
      message:
        "There is no update outcome with that id",
    }),
  },
  "update.recovery_code_unknown": {
    message: msg({
      id: "error.update.recovery_code_unknown",
      message:
        "That recovery code has expired or belongs to another confirmation. Draw the confirmation again for a new one",
    }),
  },
  "update.updater_not_newer": {
    message: msg({
      id: "error.update.updater_not_newer",
      message:
        "The updater already runs {updater_version}, which is not older than {to_version}",
    }),
  },
  "update.updater_older_than_app": {
    message: msg({
      id: "error.update.updater_older_than_app",
      message:
        "The updater of {to_version} is older than this instance, which runs {running}, so it would not accept its requests",
    }),
  },
  "update.updater_outdated": {
    message: msg({
      id: "error.update.updater_outdated",
      message:
        "The updater is too old for this container engine. Update the updater first, then try again",
    }),
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
