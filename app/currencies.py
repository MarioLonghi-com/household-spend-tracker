"""Which three-letter codes are currencies (#110).

`[A-Z]{3}` was the whole check, so a typo like `GPB` made an account in a
currency that does not exist: every figure in it was then drawn, summed and
reported apart from the pounds beside it, and nothing said why. A code is now
one ISO 4217 has assigned.

Two lists, both from ISO 4217 itself:

- **current**: list one, the codes in use today, including the funds codes
  and the X-codes (gold, SDR, the test code `XTS`) the standard assigns.
- **withdrawn**: from list three, the national currencies replaced since the
  euro began, because a household keeping the history of a Croatian or a
  Slovak account still needs to open one in kuna or koruna.

**A ledger that already holds another code keeps working.** Codes were only
ever checked for shape, so an instance may hold one that is in neither list.
Refusing it after the fact would be refusing to read that ledger; so the check
is on *new* codes only. A code the household already uses -- its base
currency, or any of its accounts' -- is accepted again as it is, and every
read path is untouched. No migration: nothing stored changes.

The lists are typed out rather than taken from a dependency: there is none in
the requirements, and ISO 4217 changes a code or two a year. When it does,
add it here; `tests/test_currencies.py` holds the shape of both lists.
"""

from __future__ import annotations

from collections.abc import Iterable


def _codes(text: str) -> frozenset[str]:
    """A block of codes as ISO prints them, space-separated, as a set."""
    return frozenset(text.split())

#: ISO 4217 list one, as amended to 2025 (XCG for ANG, ZWG for ZWL, SLE, VED).
CURRENT: frozenset[str] = _codes(
    """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV
    BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE
    CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD
    HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW
    KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR
    MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG
    QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP
    SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED
    VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU
    XTS XUA XXX YER ZAR ZMW ZWG
    """
)

#: From ISO 4217 list three: replaced since 1999, kept for accounts with history.
WITHDRAWN: frozenset[str] = _codes(
    """
    ANG ATS BEF BYR CUC CYP DEM EEK ESP FIM FRF GRD HRK IEP ITL LTL LUF LVL
    MRO MTL NLG PTE SIT SKK SLL STD TMM VEB VEF XEU ZMK ZWL
    """
)

ASSIGNED: frozenset[str] = CURRENT | WITHDRAWN


def is_assigned(code: str) -> bool:
    return code in ASSIGNED


def refusal(code: str, in_use: Iterable[str] = ()) -> str | None:
    """Why `code` cannot be used for something new, or None when it can.

    `code` is already upper-cased and three letters. `in_use` is every code the
    household holds now; one of those is never refused, whatever it is.
    """
    if code in ASSIGNED or code in set(in_use):
        return None
    return f"{code} is not an ISO 4217 currency code. Check the spelling, like GBP or EUR"
