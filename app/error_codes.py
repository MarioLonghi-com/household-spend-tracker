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
    # -- currency: app/currencies.py `check_new` (#110) ---------------------- #
    "currency.not_iso_4217": Code(
        "{code} is not an ISO 4217 currency code. Check the spelling, like GBP or EUR",
        ("code",),
    ),
    # -- money: app/money.py `parse_exact`, a typed amount read exactly ------ #
    "money.decimals_in_whole_currency": Code(
        "{value} has decimals, and {currency} has none", ("value", "currency")
    ),
    "money.not_an_amount": Code(
        "{value} is not an amount this can read. Write it with a point before the "
        "decimals and no thousands separators, like 1234.56 or -80",
        ("value",),
    ),
    "money.too_large": Code("{value} is too large to record as money", ("value",)),
    "money.too_many_decimals": Code(
        "{value} has more decimals than {currency} has ({places})",
        ("value", "currency", "places"),
    ),
    "money.too_many_digits": Code(
        "An amount {digits} digits long is too large to record as money", ("digits",)
    ),
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
    # -- split: app/services/transactions.py `split` -------------------------- #
    "split.does_not_add_up": Code(
        "The parts come to {total} and the transaction is {amount}. A split has to "
        "add up, or it moves the balance.",
        ("total", "amount", "currency"),
    ),
    # -- transfer: app/services/transactions.py `create_transfer` ------------ #
    "transfer.needs_amount_arriving": Code(
        "A {from_currency} to {to_currency} transfer needs the amount that arrives; "
        "we never invent a rate",
        ("from_currency", "to_currency"),
    ),
    "transfer.same_account": Code("An account cannot transfer to itself"),
}
