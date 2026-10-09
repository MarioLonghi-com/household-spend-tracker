"""Every code a `DomainError` may carry, with its English template (#65).

A code is the stable name the client translates from; ``detail`` stays the
English sentence a person, an agent and the logs read. The templates here are
what the client's English catalog is seeded from once Lingui lands (#53), so
they are written in ICU MessageFormat, the syntax Lingui reads: ``{name}`` is a
param. They are not used to build ``detail`` and need not match it word for
word -- ``detail`` has to stay byte-identical, and a template is free to be
written for translation.

Each entry names its params. `tests/test_error_codes.py` holds the registry and
the raise sites to each other: a code raised anywhere is registered here, a code
registered here is raised somewhere, and each raise passes exactly the params
its entry names.

**Params are raw values, never formatted text** (CLAUDE.md). Money is integer
minor units beside the ISO code of its currency, under a param called
``currency``; a date is ``YYYY-MM-DD``; an enum is its value; a name is as
stored. ``R$ 1.234,56``, ``1234,56 €`` and ``1 234,56 kr`` are one amount in
three locales, and only the client knows which one it is showing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Code:
    #: The English sentence, in ICU MessageFormat.
    template: str
    #: The params a raise must pass, by name. Money params are listed with the
    #: ``currency`` param that says how to read them.
    params: tuple[str, ...] = ()


REGISTRY: dict[str, Code] = {
    # -- backup: app/services/platform.py `delete_backup` (#165) ------------- #
    "backup.protected": Code(
        "{name} is one of the newest five update backups. They are kept so an update can be undone, and the updater removes older ones itself",
        ("name",)
    ),
    # -- category: app/services/categories.py ------------------------------- #
    "category.archived_default": Code("{name} is archived, so it cannot be a default", ("name",)),
    "category.choose_default": Code("Choose the category to always use"),
    "category.group_name_taken": Code("There is already a group called {name}", ("name",)),
    "category.group_needs_name": Code("A group needs a name"),
    "category.group_not_empty": Code(
        "A group can only be deleted when there are no categories under it. {name} still "
        "holds {held, plural, one {# category} other {# categories}}{archived, plural, =0 {} "
        "other {, # of them archived}}. Move or delete them first.",
        ("name", "held", "archived"),
    ),
    "category.group_not_found": Code("No such category group"),
    "category.in_use": Code(
        "{count, plural, one {# transaction is} other {# transactions are}} categorised as "
        "{name}. Archive it instead, and they keep their category.",
        ("count", "name"),
    ),
    "category.name_taken": Code("There is already a category called {name}", ("name",)),
    "category.needs_name": Code("A category needs a name"),
    "category.not_found": Code("No such category"),
    "category.other_household": Code("That category belongs to a different household"),
    # -- currency: app/currencies.py `check_new` (#110) ---------------------- #
    "currency.not_iso_4217": Code(
        "{code} is not an ISO 4217 currency code. Check the spelling, like GBP or EUR",
        ("code",),
    ),
    # -- money: app/money.py, an amount read from text or YNAB milliunits --- #
    "money.amount_too_large": Code("That amount is too large to record as money"),
    "money.decimals_in_whole_currency": Code(
        "{value} has decimals, and {currency} has none", ("value", "currency")
    ),
    "money.not_a_value": Code("{value} is not a monetary value", ("value",)),
    "money.not_an_amount": Code(
        "{value} is not an amount this can read. Write it with a point before the "
        "decimals and no thousands separators, like 1234.56 or -80",
        ("value",),
    ),
    "money.not_whole_minor_units": Code(
        "{milliunits} thousandths is not a whole number of {currency} minor units",
        ("milliunits", "currency"),
    ),
    "money.too_large": Code("{value} is too large to record as money", ("value",)),
    "money.too_many_decimals": Code(
        "{value} has more decimals than {currency} has ({places})",
        ("value", "currency", "places"),
    ),
    "money.too_many_digits": Code(
        "An amount {digits} digits long is too large to record as money", ("digits",)
    ),
    # -- payee: app/services/payees.py, payees and their rules -------------- #
    "payee.merge_different_households": Code("Those payees are in different households"),
    "payee.merge_into_itself": Code("A payee cannot be merged into itself"),
    "payee.needs_name": Code("A payee needs a name"),
    "payee.other_household": Code("That payee belongs to a different household"),
    "payee.rule_needs_pattern": Code("A rule needs something to match on"),
    "payee.rule_needs_payee": Code("A rule that names a payee needs a payee to point at"),
    "payee.rule_not_a_regex": Code(
        "That is not a valid regular expression: {reason}",
        ("reason",),
    ),
    "payee.rule_pattern_too_long": Code(
        "That pattern is too long (max {max} characters)",
        ("max",),
    ),
    "payee.rule_replacement_on_map": Code(
        "A replacement only means something on a rule that rewrites; this one names a payee"
    ),
    "payee.rule_replacement_too_long": Code(
        "That replacement is too long (max {max} characters)",
        ("max",),
    ),
    "payee.rule_template_no_groups": Code(
        "That replacement uses {reference}, and the pattern has no bracketed groups. Put "
        "brackets round the part you want to keep.",
        ("reference",),
    ),
    "payee.rule_template_no_such_group": Code(
        "That replacement uses {reference}, and the pattern has no group called {name}",
        ("reference", "name"),
    ),
    "payee.rule_template_too_few_groups": Code(
        "That replacement uses {reference}, and the pattern has only {groups} bracketed "
        "groups",
        ("reference", "groups"),
    ),
    # -- profile: app/services/profile.py, a member's password and factors - #
    "profile.current_factor_refused": Code(
        "That is not a working code from your current authenticator, or an unused recovery "
        "code"
    ),
    "profile.needs_authenticator": Code(
        "Set up an authenticator before making new recovery codes"
    ),
    "profile.new_code_wrong": Code(
        "That code is not right. Check the time on your phone and try again."
    ),
    "profile.recovery_grant_not_needed": Code(
        "Your current authenticator can be checked again, so prove it with a code from it (or "
        "an unused recovery code) rather than the permission from signing in."
    ),
    "profile.recovery_grant_refused": Code(
        "That permission to set up a new authenticator has expired, or is not for this "
        "session. Use an unused recovery code in its place."
    ),
    "profile.reenrolment_expired": Code("That re-enrolment has expired; start again"),
    "profile.reenrolment_not_yours": Code("That re-enrolment belongs to somebody else"),
    "profile.regenerate_refused": Code(
        "That password and authenticator code do not prove it is you"
    ),
    "profile.same_password": Code("That is already your password"),
    "profile.wrong_password": Code("That is not your current password"),
    # -- receipt: app/api/routers/agent.py, the agent's file routes (#44) ----- #
    "receipt.no_such_copy": Code("That receipt has no copy of that kind"),
    # -- reconcile: app/services/reconciling.py ------------------------------ #
    "reconcile.does_not_balance": Code(
        "That does not balance: {difference} out. Tick or untick rows until the "
        "difference is zero, or add the transaction the statement has and the "
        "register does not.",
        ("difference", "currency"),
    ),
    "reconcile.row_after_statement": Code(
        "A row dated {row_date} is after the statement closes on {statement_date}, "
        "so it cannot be on it",
        ("row_date", "statement_date"),
    ),
    # -- reimbursement: app/services/transactions.py, work expenses -------- #
    "reimbursement.not_a_work_expense_cannot_be_paid": Code(
        "A row that is not a work expense cannot have been paid back by anything"
    ),
    "reimbursement.not_money_in": Code(
        "A reimbursement is money arriving, so pick money coming in"
    ),
    "reimbursement.not_money_out": Code(
        "Only money leaving an account can be a work expense; this row is money coming in"
    ),
    "reimbursement.paid_cannot_be_written_off": Code(
        "This has been paid back. Take the payment off it before writing it off."
    ),
    "reimbursement.payment_cannot_be_expense": Code(
        "This row paid work expenses back, so it cannot be one itself"
    ),
    "reimbursement.reimburses_itself": Code("A transaction cannot reimburse itself"),
    "reimbursement.settlement_is_work_expense": Code(
        "That row is itself a work expense, so it cannot also be what paid one back"
    ),
    "reimbursement.sign_change_on_payment": Code(
        "This row paid work expenses back, so it has to stay money coming in. Take the "
        "expenses off it first if the amount really changed direction."
    ),
    "reimbursement.sign_change_on_work_expense": Code(
        "This is a work expense, so it has to stay money going out. Set it to not a work "
        "expense first if the amount really changed direction."
    ),
    "reimbursement.transfer_is_not_a_reimbursement": Code(
        "A transfer between your own accounts is not a reimbursement"
    ),
    "reimbursement.transfer_is_not_a_work_expense": Code(
        "This is one leg of a transfer, money moving between your own accounts. Flag the "
        "purchase it paid for instead."
    ),
    "reimbursement.written_off_cannot_be_paid": Code(
        "This was written off. Set it back to expected before recording a payment for it."
    ),
    # -- split: app/services/transactions.py `split` -------------------------- #
    "split.does_not_add_up": Code(
        "The parts come to {total} and the transaction is {amount}. A split has to "
        "add up, or it moves the balance.",
        ("total", "amount", "currency"),
    ),
    "split.part_count": Code("A split is between {min} and {max} parts", ("min", "max")),
    "split.repaid_work_expenses": Code(
        "This payment repaid work expenses. Take them off it before splitting it, then link "
        "each one to the part that paid it."
    ),
    "split.transfer_leg": Code(
        "This is one leg of a transfer, which is a single movement of money recorded twice. "
        "Split the other side of the transfer too, or undo it first."
    ),
    "split.work_expense_money_in": Code(
        "Every part of a work expense has to be money going out, because each part stays a "
        "work expense"
    ),
    "split.zero_part": Code("A part of a split cannot be zero"),
    # -- transaction: app/services/transactions.py -------------------------- #
    "transaction.import_line_exists": Code("That statement line is already in this account"),
    "transaction.locked": Code(
        "That transaction is locked; set it back to cleared before editing it"
    ),
    "transaction.not_found": Code("No such transaction"),
    # -- translation: app/services/translation_suggestions.py (#272) -------- #
    "translation.suggestion_not_found": Code("No such suggested wording"),
    # -- transfer: app/services/transactions.py, app/services/transfers.py - #
    "transfer.amount_not_positive": Code("A transfer amount must be positive"),
    "transfer.arriving_not_positive": Code("The amount arriving must be positive"),
    "transfer.different_households": Code("Those accounts are in different households"),
    "transfer.duplicate_leg": Code(
        "Make a new transfer from the register, not a copy of one leg of this one"
    ),
    "transfer.edit_cross_currency_leg": Code(
        "Edit each leg of a cross-currency transfer on its own; we will not re-derive a rate"
    ),
    "transfer.has_no_category": Code("A transfer has no category"),
    "transfer.link_already_a_transfer": Code(
        "One of those is already a transfer; unlink it first"
    ),
    "transfer.link_no_money": Code("A row with no money in it cannot be a transfer leg"),
    "transfer.link_repayment": Code(
        "One of those is the payment that repaid a work expense, not a transfer. Take it off "
        "the expenses it repaid first if it really is a transfer."
    ),
    "transfer.link_same_account": Code(
        "Both of those are in the same account; a transfer moves between two"
    ),
    "transfer.link_split_part": Code("A part of a split cannot be a transfer leg"),
    "transfer.link_work_expense": Code(
        "One of those is a work expense, which is money spent, not moved. Take the "
        "work-expense flag off it first if it really is a transfer."
    ),
    "transfer.needs_amount_arriving": Code(
        "A {from_currency} to {to_currency} transfer needs the amount that arrives; "
        "we never invent a rate",
        ("from_currency", "to_currency"),
    ),
    "transfer.not_a_transfer": Code("That transaction is not a transfer"),
    "transfer.pair_in_two_households": Code("Those transactions are in different households"),
    "transfer.pair_same_direction": Code(
        "A transfer takes money out of one account and into the other"
    ),
    "transfer.pair_with_itself": Code("A transaction cannot be a transfer with itself"),
    "transfer.same_account": Code("An account cannot transfer to itself"),
    "transfer.sides_differ": Code(
        "Both sides of a same-currency transfer must be the same amount"
    ),
    # -- update: app/api/routers/updates.py, the self-update endpoints (#165) - #
    "update.digest_mismatch": Code(
        "The image digests are not the ones the prepare report verified. Prepare the update again"
    ),
    "update.engine_refused": Code(
        "The updater cannot use the container engine ({socket}), so it cannot update anything",
        ("socket",)
    ),
    "update.in_flight": Code(
        "Another update request is waiting or running. Wait for it to finish, then try again"
    ),
    "update.lossy_mismatch": Code(
        "Tick every migration that cannot be undone, and only those: the update needs exactly the ones the report lists"
    ),
    "update.no_report": Code(
        "There is no prepared update with that id for this version. Check for updates and prepare it again"
    ),
    "update.no_updater": Code(
        "Updating from this screen needs the updater, and none has answered in the last two minutes"
    ),
    "update.not_a_version": Code(
        "{version} is not a release version like 1.2.3",
        ("version",)
    ),
    "update.not_newer": Code(
        "{to_version} is not newer than {running}, which this instance runs. An update never goes back",
        ("to_version", "running")
    ),
    "update.outcome_not_found": Code(
        "There is no update outcome with that id"
    ),
    "update.recovery_code_unknown": Code(
        "That recovery code has expired or belongs to another confirmation. Draw the confirmation again for a new one"
    ),
    "update.updater_not_newer": Code(
        "The updater already runs {updater_version}, which is not older than {to_version}",
        ("to_version", "updater_version")
    ),
    "update.updater_older_than_app": Code(
        "The updater of {to_version} is older than this instance, which runs {running}, so it would not accept its requests",
        ("to_version", "running")
    ),
    "update.updater_outdated": Code(
        "The updater is too old for this container engine. Update the updater first, then try again"
    ),
}
