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

export interface ErrorMessage {
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
  "auth.sign_in_first": {
    message: msg({ id: "error.auth.sign_in_first", message: "Sign in first", comment: "Refusal when the session has ended or never began" }),
  },
  "auth.owner_only": {
    message: msg({ id: "error.auth.owner_only", message: "Only the owner can do that" }),
  },
  "auth.start_again": {
    message: msg({ id: "error.auth.start_again", message: "Start again from the sign-in page" }),
  },
  "auth.secret_unreadable": {
    message: msg({ id: "error.auth.secret_unreadable", message: "That authenticator secret cannot be read with this server key" }),
  },
  "auth.too_many_for_account": {
    message: msg({ id: "error.auth.too_many_for_account", message: "Too many attempts for this account. Try again in {seconds, plural, one {# second} other {# seconds}}." }),
  },
  "auth.too_many_attempts": {
    message: msg({ id: "error.auth.too_many_attempts", message: "Too many attempts. Try again in {seconds, plural, one {# second} other {# seconds}}." }),
  },
  "auth.too_many_from_here": {
    message: msg({ id: "error.auth.too_many_from_here", message: "Too many attempts from here. Try again in {seconds, plural, one {# second} other {# seconds}}." }),
  },
  "auth.refused": {
    message: msg({ id: "error.auth.refused", message: "That email and password do not match" }),
  },
  "auth.authenticator_cleared": {
    message: msg({ id: "error.auth.authenticator_cleared", message: "This account's authenticator was reset, so there is no code to give. Use the reset link you were sent to enrol a new one, or ask an owner for a new link." }),
  },
  "auth.code_wrong": {
    message: msg({ id: "error.auth.code_wrong", message: "That code is not right, or has already been used" }),
  },
  "auth.recovery_code_wrong": {
    message: msg({ id: "error.auth.recovery_code_wrong", message: "That recovery code is not right, or has already been used" }),
  },
  "auth.new_code_wrong": {
    message: msg({ id: "error.auth.new_code_wrong", message: "That code is not right. Check the time on your phone and try again." }),
  },
  "email.not_an_address": {
    message: msg({ id: "error.email.not_an_address", message: "{email} is not an email address" }),
  },
  "email.too_long": {
    message: msg({ id: "error.email.too_long", message: "That email address is too long (max {max} characters)" }),
  },
  "email.no_name": {
    message: msg({ id: "error.email.no_name", message: "{email} has no usable name before the @" }),
  },
  "household.not_found": {
    message: msg({ id: "error.household.not_found", message: "No such household" }),
  },
  "agent_key.not_found": {
    message: msg({ id: "error.agent_key.not_found", message: "No such key", comment: "A key for a program. See GLOSSARY.md (Keys for programs)" }),
  },
  "agent_key.needs_label": {
    message: msg({ id: "error.agent_key.needs_label", message: "Give the key a label, so you know what it is for" }),
  },
  "agent_key.lifetime": {
    message: msg({ id: "error.agent_key.lifetime", message: "A key can last between a day and {max_days} days" }),
  },
  "agent_key.read_only_commit": {
    message: msg({ id: "error.agent_key.read_only_commit", message: "A read-only key has nothing to commit" }),
  },
  "agent_key.not_issuer": {
    message: msg({ id: "error.agent_key.not_issuer", message: "Only the person who issued a key can revoke it" }),
  },
  "invite.not_found": {
    message: msg({ id: "error.invite.not_found", message: "No such invitation" }),
  },
  "invite.not_an_invitation": {
    message: msg({ id: "error.invite.not_an_invitation", message: "That is not an invitation" }),
  },
  "invite.address_unusable": {
    message: msg({ id: "error.invite.address_unusable", message: "That email address cannot be used for a new account here, and this link is now spent. Ask whoever invited you for a new one." }),
  },
  "invite.owner_only": {
    message: msg({ id: "error.invite.owner_only", message: "Only an owner can invite people" }),
  },
  "invite.link_invalid": {
    message: msg({ id: "error.invite.link_invalid", message: "That invitation link is not valid" }),
  },
  "invite.owner_only_withdraw": {
    message: msg({ id: "error.invite.owner_only_withdraw", message: "Only an owner can withdraw an invitation" }),
  },
  "invite.already_used": {
    message: msg({ id: "error.invite.already_used", message: "That invitation has already been used; disable the account instead" }),
  },
  "passkey.unavailable": {
    message: msg({ id: "error.passkey.unavailable", message: "{reason, select, not_configured {Passkeys are not set up on this server} ip_address {Passkeys need a host name, and this server is configured with an IP address} wrong_host {Passkeys work only at this server's own address} insecure {Passkeys need this app opened over HTTPS} no_origins {Passkeys need the server's public address configured} other {Passkeys are not available here}}", comment: "Why passkeys cannot be used here; {reason} picks the sentence. See GLOSSARY.md (passkey)" }),
  },
  "passkey.no_user_handle": {
    message: msg({ id: "error.passkey.no_user_handle", message: "Give the member a user handle first", comment: "A user handle is the WebAuthn id of a person; an internal step" }),
  },
  "passkey.not_registered": {
    message: msg({ id: "error.passkey.not_registered", message: "That passkey could not be registered here. Start again." }),
  },
  "passkey.already_registered": {
    message: msg({ id: "error.passkey.already_registered", message: "That passkey is already registered" }),
  },
  "passkey.not_found": {
    message: msg({ id: "error.passkey.not_found", message: "No such passkey" }),
  },
  "passkey.needs_name": {
    message: msg({ id: "error.passkey.needs_name", message: "A passkey needs a name" }),
  },
  "passkey.too_many_at_once": {
    message: msg({ id: "error.passkey.too_many_at_once", message: "Too many sign-ins at once. Try again in a minute." }),
  },
  "passkey.refused": {
    message: msg({ id: "error.passkey.refused", message: "That passkey cannot sign in here" }),
  },
  "passkey.not_an_answer": {
    message: msg({ id: "error.passkey.not_an_answer", message: "That is not a passkey answer" }),
  },
  "password.too_short": {
    message: msg({ id: "error.password.too_short", message: "That password is too short: it needs at least {min_length} characters" }),
  },
  "password.common": {
    message: msg({ id: "error.password.common", message: "That password is one of the {count, number} most common passwords, which are the first ones anybody guessing tries" }),
  },
  "password.is_email": {
    message: msg({ id: "error.password.is_email", message: "A password cannot be your email address" }),
  },
  "password.too_short_and_is_email": {
    message: msg({ id: "error.password.too_short_and_is_email", message: "That password needs at least {min_length} characters, and it cannot be your email address" }),
  },
  "password.common_and_is_email": {
    message: msg({ id: "error.password.common_and_is_email", message: "That password is one of the {count, number} most common passwords, and it cannot be your email address" }),
  },
  "reset.not_authenticator": {
    message: msg({ id: "error.reset.not_authenticator", message: "This link does not change the authenticator" }),
  },
  "reset.offer_expired": {
    message: msg({ id: "error.reset.offer_expired", message: "That authenticator offer has expired; scan a new one", comment: "The QR code offered for setting up a new authenticator" }),
  },
  "reset.choose_what": {
    message: msg({ id: "error.reset.choose_what", message: "Choose what to reset: the password, the authenticator, or both" }),
  },
  "reset.owner_only": {
    message: msg({ id: "error.reset.owner_only", message: "Only an owner can reset an account" }),
  },
  "reset.own_account": {
    message: msg({ id: "error.reset.own_account", message: "You cannot reset your own account here: change your password or your authenticator from your profile" }),
  },
  "reset.account_disabled": {
    message: msg({ id: "error.reset.account_disabled", message: "That account is disabled, so a reset link for it could not be followed. Re-enable it first." }),
  },
  "reset.not_found": {
    message: msg({ id: "error.reset.not_found", message: "No such reset link" }),
  },
  "reset.link_invalid": {
    message: msg({ id: "error.reset.link_invalid", message: "That reset link is not valid" }),
  },
  "reset.overtaken": {
    message: msg({ id: "error.reset.overtaken", message: "That reset link was used or replaced a moment ago. Look at the account again before doing anything else to it" }),
  },
  "reset.owner_only_withdraw": {
    message: msg({ id: "error.reset.owner_only_withdraw", message: "Only an owner can withdraw a reset link" }),
  },
  "reset.choose_password": {
    message: msg({ id: "error.reset.choose_password", message: "Choose a new password" }),
  },
  "reset.not_password": {
    message: msg({ id: "error.reset.not_password", message: "This link does not change the password" }),
  },
  "reset.enrol_first": {
    message: msg({ id: "error.reset.enrol_first", message: "Enrol a new authenticator first" }),
  },
  "setup.already_done": {
    message: msg({ id: "error.setup.already_done", message: "This instance has already been set up" }),
  },
  "setup.is_an_invitation": {
    message: msg({ id: "error.setup.is_an_invitation", message: "That is an invitation; finish it from the link you were sent" }),
  },
  "setup.session_expired": {
    message: msg({ id: "error.setup.session_expired", message: "That setup session has expired; start again" }),
  },
  "setup.store_codes": {
    message: msg({ id: "error.setup.store_codes", message: "Store your recovery codes somewhere that is not this browser, then tick the box" }),
  },
  "setup.not_waiting": {
    message: msg({ id: "error.setup.not_waiting", message: "This instance is not waiting to be set up" }),
  },
  "setup.token_wrong": {
    message: msg({ id: "error.setup.token_wrong", message: "That setup token is not right" }),
  },
  "setup.server_restarted": {
    message: msg({ id: "error.setup.server_restarted", message: "The server restarted; start again with the new setup token" }),
  },
  "setup.needs_display_name": {
    message: msg({ id: "error.setup.needs_display_name", message: "A display name is required" }),
  },
  "setup.enrol_first": {
    message: msg({ id: "error.setup.enrol_first", message: "Finish enrolling an authenticator first" }),
  },
  "setup.address_taken": {
    message: msg({ id: "error.setup.address_taken", message: "Somebody already uses that email address" }),
  },
  "setup.session_invalid": {
    message: msg({ id: "error.setup.session_invalid", message: "That setup session is not valid; start again" }),
  },
  "stepup.refused": {
    message: msg({ id: "error.stepup.refused", message: "That password and code do not match" }),
  },
  "stepup.needed": {
    message: msg({ id: "error.stepup.needed", message: "Confirm your password and authenticator code first", comment: "See GLOSSARY.md (step-up)" }),
  },
  "user.not_found": {
    message: msg({ id: "error.user.not_found", message: "No such person" }),
  },
  "user.cannot_disable_self": {
    message: msg({ id: "error.user.cannot_disable_self", message: "You cannot disable yourself; there would be nobody left to undo it" }),
  },
  "user.no_owner_left": {
    message: msg({ id: "error.user.no_owner_left", message: "There would be no owner left" }),
  },
  "import.file_too_large": {
    message: msg({ id: "error.import.file_too_large", message: "That file is larger than this is meant for (at most {max_bytes, number} bytes)" }),
  },
  "import.line_not_found": {
    message: msg({ id: "error.import.line_not_found", message: "No such line on this import" }),
  },
  "import.line_needs_category": {
    message: msg({ id: "error.import.line_needs_category", message: "Give the line a category first" }),
  },
  "import.line_has_no_payee": {
    message: msg({ id: "error.import.line_has_no_payee", message: "This line has no payee to attach a rule to" }),
  },
  "import.send_something": {
    message: msg({ id: "error.import.send_something", message: "Send a file or some pasted text" }),
  },
  "import.already_staged": {
    message: msg({ id: "error.import.already_staged", message: "This exact file is already staged for {account}, from {at} (as {filename}), and is waiting to be reviewed. Open that import rather than starting a second one, or send this with force set.", comment: "{at} is when the earlier import was made; force is the name of a request field" }),
    dates: ["at"],
  },
  "import.already_imported": {
    message: msg({ id: "error.import.already_imported", message: "This exact file was already imported into {account} on {at} (as {filename}). Nothing has been changed. If you meant to import it again, send it with force set.", comment: "force is the name of a request field" }),
    dates: ["at"],
  },
  "import.choose_product": {
    message: msg({ id: "error.import.choose_product", message: "This file holds more than one account ({products}), and {account} has not said which of them it is. Set its statement product on the Accounts screen, then read the file again.", comment: "{products} is the bank's own names for the accounts in the file" }),
  },
  "import.product_absent": {
    message: msg({ id: "error.import.product_absent", message: "{account} takes the {product} rows of a statement, and this file has none: it holds {products}." }),
  },
  "import.wrong_currency": {
    message: msg({ id: "error.import.wrong_currency", message: "This file is in {currencies}, and {account} holds {currency}: none of its rows are in {currency}. Import it into an account that holds {currencies}, or check that this is the right file." }),
  },
  "import.not_awaiting_commit": {
    message: msg({ id: "error.import.not_awaiting_commit", message: "That import is {status}, not waiting to be committed", comment: "{status} is the import's state as stored: applied, preview, failed, undone" }),
  },
  "import.not_found": {
    message: msg({ id: "error.import.not_found", message: "No such import" }),
  },
  "import.not_an_import": {
    message: msg({ id: "error.import.not_an_import", message: "That batch is not an import", comment: "Batch: one act in History. See GLOSSARY.md" }),
  },
  "import.not_staged": {
    message: msg({ id: "error.import.not_staged", message: "That import is {status}, not staged", comment: "{status} is the import's state as stored" }),
  },
  "import.committed_not_purged": {
    message: msg({ id: "error.import.committed_not_purged", message: "That import is {status}, not staged. A committed import is put back from History rather than purged." }),
  },
  "import.line_no_own_category": {
    message: msg({ id: "error.import.line_no_own_category", message: "That line has no category of its own to apply" }),
  },
  "import.line_already_in_account": {
    message: msg({ id: "error.import.line_already_in_account", message: "That statement line is already in this account" }),
  },
  "history.no_such_table": {
    message: msg({ id: "error.history.no_such_table", message: "No such table in the audit log" }),
  },
  "history.batch_not_found": {
    message: msg({ id: "error.history.batch_not_found", message: "No such batch", comment: "Batch: one act in History. See GLOSSARY.md" }),
  },
  "upload.too_large": {
    message: msg({ id: "error.upload.too_large", message: "That file is too large (at most {max_bytes, number} bytes)" }),
  },
  "account_import.rows_refused": {
    message: msg({ id: "error.account_import.rows_refused", message: "{refused} of {rows, plural, one {# row} other {# rows}} cannot be imported, so none were. Nothing has been changed; correct the file and try again." }),
  },
  "account_import.no_accounts": {
    message: msg({ id: "error.account_import.no_accounts", message: "There are no accounts in this file" }),
  },
  "account_import.unknown_column": {
    message: msg({ id: "error.account_import.unknown_column", message: "This file has a column this does not know: {columns}. The columns are {known} -- download the template to start from them.", comment: "{columns} and {known} are column names, which stay in English" }),
  },
  "account_import.missing_column": {
    message: msg({ id: "error.account_import.missing_column", message: "This file has no {columns} column, and every account needs one. The first line should name the columns, as the template does." }),
  },
  "account_import.not_csv": {
    message: msg({ id: "error.account_import.not_csv", message: "Line {line} of this file cannot be read as CSV. Save it from the spreadsheet as CSV again, or start from the template." }),
  },
  "account_import.column_twice": {
    message: msg({ id: "error.account_import.column_twice", message: "This file has the column {column} twice" }),
  },
  "account_import.too_many_rows": {
    message: msg({ id: "error.account_import.too_many_rows", message: "This file has more than {max} accounts in it, which is more than a household has -- it may be a statement rather than a list of accounts" }),
  },
  "ynab.say_how": {
    message: msg({ id: "error.ynab.say_how", message: "Say how to reach YNAB: through its export file or its API" }),
  },
  "ynab.token_needed": {
    message: msg({ id: "error.ynab.token_needed", message: "A YNAB personal access token is needed" }),
  },
  "ynab.token_malformed": {
    message: msg({ id: "error.ynab.token_malformed", message: "That is not a YNAB personal access token" }),
  },
  "ynab.choose_export": {
    message: msg({ id: "error.ynab.choose_export", message: "Choose the YNAB export: the zip, or its Register.csv" }),
  },
  "ynab.choose_plan": {
    message: msg({ id: "error.ynab.choose_plan", message: "Choose which YNAB plan to import", comment: "A plan is what YNAB calls a budget" }),
  },
  "ynab.plan_unreadable": {
    message: msg({ id: "error.ynab.plan_unreadable", message: "The import plan cannot be read", comment: "The import plan: the choices made on the One-time Import screen" }),
  },
  "ynab.date_format_unknown": {
    message: msg({ id: "error.ynab.date_format_unknown", message: "{format} is not a date format this import reads" }),
  },
  "ynab.currency_not_a_code": {
    message: msg({ id: "error.ynab.currency_not_a_code", message: "{currency} is not a three-letter currency code" }),
  },
  "ynab.flags_choice": {
    message: msg({ id: "error.ynab.flags_choice", message: "Flags are either kept in the memo or ignored", comment: "YNAB's coloured flags on a transaction" }),
  },
  "ynab.starting_balance_choice": {
    message: msg({ id: "error.ynab.starting_balance_choice", message: "The starting balance is either imported or skipped" }),
  },
  "ynab.range_backwards": {
    message: msg({ id: "error.ynab.range_backwards", message: "The date range ends before it starts" }),
  },
  "ynab.confirm_states": {
    message: msg({ id: "error.ynab.confirm_states", message: "Confirm that YNAB's reconciled and cleared states are reset: everything arrives uncleared", comment: "See GLOSSARY.md (Cleared, Uncleared, reconcile)" }),
  },
  "ynab.date_unreadable": {
    message: msg({ id: "error.ynab.date_unreadable", message: "{date} is not a {format} date", comment: "{format} is a date pattern like DD/MM/YYYY" }),
  },
  "ynab.account_undecided": {
    message: msg({ id: "error.ynab.account_undecided", message: "Say what to do with the YNAB account {account}" }),
  },
  "ynab.category_undecided": {
    message: msg({ id: "error.ynab.category_undecided", message: "Say what to do with the YNAB category {category}" }),
  },
  "ynab.account_elsewhere": {
    message: msg({ id: "error.ynab.account_elsewhere", message: "The account chosen for {account} is not in this household" }),
  },
  "ynab.account_currency": {
    message: msg({ id: "error.ynab.account_currency", message: "{account} is in {account_currency}, and this plan is in {currency}" }),
  },
  "ynab.account_twice": {
    message: msg({ id: "error.ynab.account_twice", message: "{first} and {second} both go to {account}; each YNAB account needs an account of its own" }),
  },
  "ynab.category_elsewhere": {
    message: msg({ id: "error.ynab.category_elsewhere", message: "The category chosen for {category} is not in this household" }),
  },
  "ynab.rows_taken": {
    message: msg({ id: "error.ynab.rows_taken", message: "Some of these rows were imported by another request a moment ago; open the Import screen and check History before trying again" }),
  },
  "ynab.new_account_needs_name": {
    message: msg({ id: "error.ynab.new_account_needs_name", message: "The new account for {account} needs a name" }),
  },
  "ynab.account_mapping_unknown": {
    message: msg({ id: "error.ynab.account_mapping_unknown", message: "{kind} is not something an account can be mapped to" }),
  },
  "ynab.new_category_needs_name": {
    message: msg({ id: "error.ynab.new_category_needs_name", message: "The new category for {category} needs a name" }),
  },
  "ynab.category_mapping_unknown": {
    message: msg({ id: "error.ynab.category_mapping_unknown", message: "{kind} is not something a category can be mapped to" }),
  },
  "ynab.account_type_unknown": {
    message: msg({ id: "error.ynab.account_type_unknown", message: "{type} is not an account type" }),
  },
  "ynab.answer_unreadable": {
    message: msg({ id: "error.ynab.answer_unreadable", message: "YNAB's answer could not be read" }),
  },
  "ynab.answer_too_large": {
    message: msg({ id: "error.ynab.answer_too_large", message: "YNAB's answer was larger than this import will read" }),
  },
  "ynab.timeout": {
    message: msg({ id: "error.ynab.timeout", message: "YNAB took too long to answer; try again later" }),
  },
  "ynab.status": {
    message: msg({ id: "error.ynab.status", message: "YNAB answered {status}; try again later", comment: "{status} is an HTTP status number, like 503" }),
  },
  "ynab.unreachable": {
    message: msg({ id: "error.ynab.unreachable", message: "YNAB could not be reached; check the connection and try again" }),
  },
  "ynab.token_rejected": {
    message: msg({ id: "error.ynab.token_rejected", message: "YNAB rejected the token" }),
  },
  "ynab.no_such_plan": {
    message: msg({ id: "error.ynab.no_such_plan", message: "YNAB has no such plan for this token" }),
  },
  "ynab.rate_limited": {
    message: msg({ id: "error.ynab.rate_limited", message: "YNAB is limiting requests from this token for now; try again in an hour" }),
  },
  "ynab.no_transactions": {
    message: msg({ id: "error.ynab.no_transactions", message: "There are no transactions in this file" }),
  },
  "ynab.plan_not_register": {
    message: msg({ id: "error.ynab.plan_not_register", message: "Only the YNAB Register.csv is needed, not the Plan.csv" }),
  },
  "ynab.not_register": {
    message: msg({ id: "error.ynab.not_register", message: "This is not a YNAB Register.csv: it has no {columns} column. Export the plan from YNAB and upload the zip or its Register.csv." }),
  },
  "ynab.zip_unreadable": {
    message: msg({ id: "error.ynab.zip_unreadable", message: "That zip file cannot be opened" }),
  },
  "ynab.zip_has_no_register": {
    message: msg({ id: "error.ynab.zip_has_no_register", message: "Only the YNAB Register.csv is needed, not the Plan.csv, and this zip has no Register.csv in it" }),
  },
  "ynab.register_too_large": {
    message: msg({ id: "error.ynab.register_too_large", message: "The Register.csv in that zip is larger than this import reads" }),
  },
  "ynab.not_csv": {
    message: msg({ id: "error.ynab.not_csv", message: "Line {line} of this file cannot be read as CSV. Export the plan from YNAB again and upload the zip or its Register.csv." }),
  },
  "ynab.amount_too_long": {
    message: msg({ id: "error.ynab.amount_too_long", message: "Line {line} of this file has an amount longer than {max} characters, which YNAB never writes -- it may not be a YNAB export, or it may be damaged" }),
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
