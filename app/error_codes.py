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
    # -- auth: app/auth/service.py, ratelimit.py, crypto.py, app/api/deps.py, sign-in (#267) --- #
    "auth.sign_in_first": Code("Sign in first"),
    "auth.owner_only": Code("Only the owner can do that"),
    "auth.start_again": Code("Start again from the sign-in page"),
    "auth.secret_unreadable": Code("That authenticator secret cannot be read with this server key"),
    "auth.too_many_for_account": Code("Too many attempts for this account. Try again in {seconds, plural, one {# second} other {# seconds}}.", ("seconds",)),
    "auth.too_many_attempts": Code("Too many attempts. Try again in {seconds, plural, one {# second} other {# seconds}}.", ("seconds",)),
    "auth.too_many_from_here": Code("Too many attempts from here. Try again in {seconds, plural, one {# second} other {# seconds}}.", ("seconds",)),
    "auth.refused": Code("That email and password do not match"),
    "auth.authenticator_cleared": Code("This account's authenticator was reset, so there is no code to give. Use the reset link you were sent to enrol a new one, or ask an owner for a new link."),
    "auth.code_wrong": Code("That code is not right, or has already been used"),
    "auth.recovery_code_wrong": Code("That recovery code is not right, or has already been used"),
    "auth.new_code_wrong": Code("That code is not right. Check the time on your phone and try again."),
    # -- email: app/auth/email_canonical.py (#267) ------------------------------- #
    "email.not_an_address": Code("{email} is not an email address", ("email",)),
    "email.too_long": Code("That email address is too long (max {max} characters)", ("max",)),
    "email.no_name": Code("{email} has no usable name before the @", ("email",)),
    # -- household, agent_key: app/services/agent_keys.py, app/api/routers/profile.py (#267) --- #
    "household.not_found": Code("No such household"),
    "agent_key.not_found": Code("No such key"),
    "agent_key.needs_label": Code("Give the key a label, so you know what it is for"),
    "agent_key.lifetime": Code("A key can last between a day and {max_days} days", ("max_days",)),
    "agent_key.read_only_commit": Code("A read-only key has nothing to commit"),
    "agent_key.not_issuer": Code("Only the person who issued a key can revoke it"),
    # -- invite: app/services/invitations.py, app/api/routers/invites.py (#267) --- #
    "invite.not_found": Code("No such invitation"),
    "invite.not_an_invitation": Code("That is not an invitation"),
    "invite.address_unusable": Code("That email address cannot be used for a new account here, and this link is now spent. Ask whoever invited you for a new one."),
    "invite.owner_only": Code("Only an owner can invite people"),
    "invite.link_invalid": Code("That invitation link is not valid"),
    "invite.owner_only_withdraw": Code("Only an owner can withdraw an invitation"),
    "invite.already_used": Code("That invitation has already been used; disable the account instead"),
    # -- passkey: app/auth/passkeys.py (#267) ------------------------------------ #
    "passkey.unavailable": Code("{reason, select, not_configured {Passkeys are not set up on this server} ip_address {Passkeys need a host name, and this server is configured with an IP address} wrong_host {Passkeys work only at this server's own address} insecure {Passkeys need this app opened over HTTPS} no_origins {Passkeys need the server's public address configured} other {Passkeys are not available here}}", ("reason",)),
    "passkey.no_user_handle": Code("Give the member a user handle first"),
    "passkey.not_registered": Code("That passkey could not be registered here. Start again."),
    "passkey.already_registered": Code("That passkey is already registered"),
    "passkey.not_found": Code("No such passkey"),
    "passkey.needs_name": Code("A passkey needs a name"),
    "passkey.too_many_at_once": Code("Too many sign-ins at once. Try again in a minute."),
    "passkey.refused": Code("That passkey cannot sign in here"),
    "passkey.not_an_answer": Code("That is not a passkey answer"),
    # -- password: app/auth/passwords.py `refuse_weak` (#267) -------------------- #
    "password.too_short": Code("That password is too short: it needs at least {min_length} characters", ("min_length",)),
    "password.common": Code("That password is one of the {count, number} most common passwords, which are the first ones anybody guessing tries", ("count",)),
    "password.is_email": Code("A password cannot be your email address"),
    "password.too_short_and_is_email": Code("That password needs at least {min_length} characters, and it cannot be your email address", ("min_length",)),
    "password.common_and_is_email": Code("That password is one of the {count, number} most common passwords, and it cannot be your email address", ("count",)),
    # -- reset: app/services/account_resets.py, app/api/routers/account_resets.py (#267) --- #
    "reset.not_authenticator": Code("This link does not change the authenticator"),
    "reset.offer_expired": Code("That authenticator offer has expired; scan a new one"),
    "reset.choose_what": Code("Choose what to reset: the password, the authenticator, or both"),
    "reset.owner_only": Code("Only an owner can reset an account"),
    "reset.own_account": Code("You cannot reset your own account here: change your password or your authenticator from your profile"),
    "reset.account_disabled": Code("That account is disabled, so a reset link for it could not be followed. Re-enable it first."),
    "reset.not_found": Code("No such reset link"),
    "reset.link_invalid": Code("That reset link is not valid"),
    "reset.overtaken": Code("That reset link was used or replaced a moment ago. Look at the account again before doing anything else to it"),
    "reset.owner_only_withdraw": Code("Only an owner can withdraw a reset link"),
    "reset.choose_password": Code("Choose a new password"),
    "reset.not_password": Code("This link does not change the password"),
    "reset.enrol_first": Code("Enrol a new authenticator first"),
    # -- setup: app/auth/setup.py, app/auth/crypto.py, app/api/routers/setup.py (#267) --- #
    "setup.already_done": Code("This instance has already been set up"),
    "setup.is_an_invitation": Code("That is an invitation; finish it from the link you were sent"),
    "setup.session_expired": Code("That setup session has expired; start again"),
    "setup.store_codes": Code("Store your recovery codes somewhere that is not this browser, then tick the box"),
    "setup.not_waiting": Code("This instance is not waiting to be set up"),
    "setup.token_wrong": Code("That setup token is not right"),
    "setup.server_restarted": Code("The server restarted; start again with the new setup token"),
    "setup.needs_display_name": Code("A display name is required"),
    "setup.enrol_first": Code("Finish enrolling an authenticator first"),
    "setup.address_taken": Code("Somebody already uses that email address"),
    "setup.session_invalid": Code("That setup session is not valid; start again"),
    # -- stepup: app/auth/stepup.py (#267) --------------------------------------- #
    "stepup.refused": Code("That password and code do not match"),
    "stepup.needed": Code("Confirm your password and authenticator code first"),
    # -- user: app/services/users.py (#267) -------------------------------------- #
    "user.not_found": Code("No such person"),
    "user.cannot_disable_self": Code("You cannot disable yourself; there would be nobody left to undo it"),
    "user.no_owner_left": Code("There would be no owner left"),
    # -- import: app/api/routers/imports.py, app/services/importing.py, a statement import (#267) --- #
    "import.file_too_large": Code("That file is larger than this is meant for (at most {max_bytes, number} bytes)", ("max_bytes",)),
    "import.line_not_found": Code("No such line on this import"),
    "import.line_needs_category": Code("Give the line a category first"),
    "import.line_has_no_payee": Code("This line has no payee to attach a rule to"),
    "import.send_something": Code("Send a file or some pasted text"),
    "import.already_staged": Code("This exact file is already staged for {account}, from {at} (as {filename}), and is waiting to be reviewed. Open that import rather than starting a second one, or send this with force set.", ("account", "at", "filename")),
    "import.already_imported": Code("This exact file was already imported into {account} on {at} (as {filename}). Nothing has been changed. If you meant to import it again, send it with force set.", ("account", "at", "filename")),
    "import.choose_product": Code("This file holds more than one account ({products}), and {account} has not said which of them it is. Set its statement product on the Accounts screen, then read the file again.", ("account", "products")),
    "import.product_absent": Code("{account} takes the {product} rows of a statement, and this file has none: it holds {products}.", ("account", "product", "products")),
    "import.wrong_currency": Code("This file is in {currencies}, and {account} holds {currency}: none of its rows are in {currency}. Import it into an account that holds {currencies}, or check that this is the right file.", ("account", "currency", "currencies")),
    "import.not_awaiting_commit": Code("That import is {status}, not waiting to be committed", ("status",)),
    "import.not_found": Code("No such import"),
    "import.not_an_import": Code("That batch is not an import"),
    "import.not_staged": Code("That import is {status}, not staged", ("status",)),
    "import.committed_not_purged": Code("That import is {status}, not staged. A committed import is put back from History rather than purged.", ("status",)),
    "import.line_no_own_category": Code("That line has no category of its own to apply"),
    "import.line_already_in_account": Code("That statement line is already in this account"),
    # -- history, upload: app/api/routers/imports.py, app/api/uploads.py (#267) --- #
    "history.no_such_table": Code("No such table in the audit log"),
    "history.batch_not_found": Code("No such batch"),
    "upload.too_large": Code("That file is too large (at most {max_bytes, number} bytes)", ("max_bytes",)),
    # -- account_import: app/services/account_import.py, accounts from a CSV (#267) --- #
    "account_import.rows_refused": Code("{refused} of {rows, plural, one {# row} other {# rows}} cannot be imported, so none were. Nothing has been changed; correct the file and try again.", ("refused", "rows")),
    "account_import.no_accounts": Code("There are no accounts in this file"),
    "account_import.unknown_column": Code("This file has a column this does not know: {columns}. The columns are {known} -- download the template to start from them.", ("columns", "known")),
    "account_import.missing_column": Code("This file has no {columns} column, and every account needs one. The first line should name the columns, as the template does.", ("columns",)),
    "account_import.not_csv": Code("Line {line} of this file cannot be read as CSV. Save it from the spreadsheet as CSV again, or start from the template.", ("line",)),
    "account_import.column_twice": Code("This file has the column {column} twice", ("column",)),
    "account_import.too_many_rows": Code("This file has more than {max} accounts in it, which is more than a household has -- it may be a statement rather than a list of accounts", ("max",)),
    # -- ynab: app/services/one_time_import/, app/api/routers/one_time_import.py, One-time Import (#267) --- #
    "ynab.say_how": Code("Say how to reach YNAB: through its export file or its API"),
    "ynab.token_needed": Code("A YNAB personal access token is needed"),
    "ynab.token_malformed": Code("That is not a YNAB personal access token"),
    "ynab.choose_export": Code("Choose the YNAB export: the zip, or its Register.csv"),
    "ynab.choose_plan": Code("Choose which YNAB plan to import"),
    "ynab.plan_unreadable": Code("The import plan cannot be read"),
    "ynab.date_format_unknown": Code("{format} is not a date format this import reads", ("format",)),
    "ynab.currency_not_a_code": Code("{currency} is not a three-letter currency code", ("currency",)),
    "ynab.flags_choice": Code("Flags are either kept in the memo or ignored"),
    "ynab.starting_balance_choice": Code("The starting balance is either imported or skipped"),
    "ynab.range_backwards": Code("The date range ends before it starts"),
    "ynab.confirm_states": Code("Confirm that YNAB's reconciled and cleared states are reset: everything arrives uncleared"),
    "ynab.date_unreadable": Code("{date} is not a {format} date", ("date", "format")),
    "ynab.account_undecided": Code("Say what to do with the YNAB account {account}", ("account",)),
    "ynab.category_undecided": Code("Say what to do with the YNAB category {category}", ("category",)),
    "ynab.account_elsewhere": Code("The account chosen for {account} is not in this household", ("account",)),
    "ynab.account_currency": Code("{account} is in {account_currency}, and this plan is in {currency}", ("account", "account_currency", "currency")),
    "ynab.account_twice": Code("{first} and {second} both go to {account}; each YNAB account needs an account of its own", ("first", "second", "account")),
    "ynab.category_elsewhere": Code("The category chosen for {category} is not in this household", ("category",)),
    "ynab.rows_taken": Code("Some of these rows were imported by another request a moment ago; open the Import screen and check History before trying again"),
    "ynab.new_account_needs_name": Code("The new account for {account} needs a name", ("account",)),
    "ynab.account_mapping_unknown": Code("{kind} is not something an account can be mapped to", ("kind",)),
    "ynab.new_category_needs_name": Code("The new category for {category} needs a name", ("category",)),
    "ynab.category_mapping_unknown": Code("{kind} is not something a category can be mapped to", ("kind",)),
    "ynab.account_type_unknown": Code("{type} is not an account type", ("type",)),
    "ynab.answer_unreadable": Code("YNAB's answer could not be read"),
    "ynab.answer_too_large": Code("YNAB's answer was larger than this import will read"),
    "ynab.timeout": Code("YNAB took too long to answer; try again later"),
    "ynab.status": Code("YNAB answered {status}; try again later", ("status",)),
    "ynab.unreachable": Code("YNAB could not be reached; check the connection and try again"),
    "ynab.token_rejected": Code("YNAB rejected the token"),
    "ynab.no_such_plan": Code("YNAB has no such plan for this token"),
    "ynab.rate_limited": Code("YNAB is limiting requests from this token for now; try again in an hour"),
    "ynab.no_transactions": Code("There are no transactions in this file"),
    "ynab.plan_not_register": Code("Only the YNAB Register.csv is needed, not the Plan.csv"),
    "ynab.not_register": Code("This is not a YNAB Register.csv: it has no {columns} column. Export the plan from YNAB and upload the zip or its Register.csv.", ("columns",)),
    "ynab.zip_unreadable": Code("That zip file cannot be opened"),
    "ynab.zip_has_no_register": Code("Only the YNAB Register.csv is needed, not the Plan.csv, and this zip has no Register.csv in it"),
    "ynab.register_too_large": Code("The Register.csv in that zip is larger than this import reads"),
    "ynab.not_csv": Code("Line {line} of this file cannot be read as CSV. Export the plan from YNAB again and upload the zip or its Register.csv.", ("line",)),
    "ynab.amount_too_long": Code("Line {line} of this file has an amount longer than {max} characters, which YNAB never writes -- it may not be a YNAB export, or it may be damaged", ("line", "max")),
}
