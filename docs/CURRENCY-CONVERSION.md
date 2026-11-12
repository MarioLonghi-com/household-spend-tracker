# How the tracker converts currencies

The tracker keeps every amount in its account's currency and never changes
it. A report may show figures in the household's **base currency**, and when
it does it converts at read time, at rates the household chose, and says which
rate and date each figure used. Nothing converted is ever stored.

This page is the reference for that: where rates come from, which one a figure
uses, how it is rounded, and how old the rates are. Issue #396 is the design.

## What is and is not converted

| | Converted? |
|---|---|
| Transactions, the Register, balances, the Accounts screen | **Never.** Each stays in its account's currency. |
| A report that offers "in base currency" | Yes, at read time. Every converted figure is marked "≈". |
| An amount in the base currency already | Nothing to do: it converts to itself at 1. |
| An amount with no rate at all | **Not converted.** It is shown in its own currency, listed under "Not converted", and left out of totals. A missing rate is never treated as 0 or 1. |

Totals are built by converting each row or account first and adding the
converted figures, never by converting a sum of two currencies.

### Income vs Expense, "All, in {base}"

The first report that converts. Its currency toggle offers **All, in
{base}** beside the currencies whenever the household holds one other than
the base, and opens on it once every such currency has a rate. Each row is
converted at its own date's rate under the household's policy, then added;
the rows behind a figure show each one's own amount, rate and converted
amount, and add up to the figure. A repayment's surplus or shortfall is
worked out in the claim's own currency first and its rows converted at the
repayment's rate; a claim whose expenses are in another currency than its
repayment is not converted yet. **Latest 12 months** on the Reports index is
this view over the twelve closed months.

## Where rates come from

Each household has a **chain**: an ordered list of sources, set on
*Household → Exchange rates*. The default is **manual, then ECB**.

| Source | What it is |
|---|---|
| `manual` | Rates a member typed or imported from a CSV file. The household's own data: in History, and undone there. |
| `ecb` | The European Central Bank's euro reference rates: about thirty currencies, every working day, history back to 1999. Shared by every household on the instance. |
| `own` | The rates the household's own linked transfers between two currencies imply. Off by default. They include the bank's spread and fees and exist only on the days money was exchanged, so putting them in the chain mixes kinds of rate; the page warns when you do. They are always shown *beside* the rate in use, for comparison, when *Show my own rates* is on. |

An empty chain is allowed: then nothing converts, and every report shows one
currency at a time.

### Fetching: only when someone presses the button

**The tracker makes no request on its own.** There is no schedule and no
fetch when a page loads. ECB rates arrive only when a member presses **Fetch
now** on the Exchange rates page, and that press is recorded: who, when, what
was asked for, how many rates came back.

- *Fetch now* asks for every date not stored yet: the first time from the
  earliest transaction in each currency, afterwards from the last stored rate.
  It asks only for currencies the ECB publishes: one it never has is listed
  under *Not covered* instead, and one it stopped publishing (RUB, HRK, BGN,
  the currencies the euro replaced) is complete once its last published day
  is stored.
- *Refetch a range* asks for the dates you give and replaces what it fetches.
  It is for repairing a bad download.
- One fetch runs at a time on an instance; a second press waits its turn.
  A fetch always ends, as done or failed; one the application stopped
  working on reads as interrupted and does not hold up the next press.
- A failed fetch keeps every rate already stored and changes no report.

**The one host contacted** is `data-api.ecb.europa.eu` (HTTPS). Allow it on a
firewalled install if you want ECB rates. The request carries currency codes
and dates and nothing else: no household data, no address, no identifying
user agent. Without network access the tracker works on manual rates alone.

### Entering rates by hand

A rate is **units of the currency per 1 unit of the base currency** -- the
ECB's convention. With a EUR base, a GBP rate of `0.8346` means 1 EUR =
0.8346 GBP; the form shows the inverse, 1 GBP = 1.1982 EUR, as a check.

A rate is for one day (`2026-10-30`) or a whole month (`2026-10`). A file of
them has three columns, `currency,date,rate`, with a dot for the decimals and
no thousands separator. The file is all or none: a dry run shows every line's
verdict, and one line with a problem keeps the whole file out.

## Which rate a figure uses

**The chain.** The sources are tried in order. The first with a rate within
the household's stale limit (7 days by default) wins. If none has one, the
newest rate any source has is used and the figure is flagged *stale*. If none
has any rate, the figure is not converted.

**The policy** decides which date's rate:

| Policy | Rate used |
|---|---|
| Daily rate per row (`spot`) | The last rate on or before the figure's date. Weekends and holidays take the last working day's, and show its date. |
| Monthly average (`month_avg`) | The mean of the month's daily rates. A monthly manual rate is the month's figure. **The default** for monthly figures. |
| Month-end rate (`month_end`) | The last rate on or before the month's last day. |

**Cross rates.** The ECB quotes against the euro. For another base currency
the rate is crossed through the euro on the same date:

    rate(SEK per GBP) = rate(SEK per EUR) / rate(GBP per EUR)

The figure says the rate went *via EUR*.

**Rounding.** Rates are decimals, never floating point, and nothing in
between is rounded. The one rounding is the converted amount, half-up at the
base currency's own number of decimals -- the rule every typed amount already
follows: 0 for JPY, 2 for EUR, 3 for KWD.

    SEK 1,000.00 on 30 Oct 2026, base GBP:
      1000 / (10.978 / 0.8338) = 75.9519... -> GBP 75.95

## How old the rates are

Because nothing refreshes rates unasked, every page that shows a converted
figure carries one line, **Rates as of**, above its first figure:

- *Rates as of 7 Nov 2026 · ECB · fetched 9 Nov 2026 14:02 by Robin*
- *Rates as of 24 Oct 2026, 16 days old · fetch them on the Exchange rates page*
- *Fetching rates…*
- *Rates as of 24 Oct 2026 · the last fetch failed on 9 Nov 2026: …*
- *No exchange rates yet · fetch them on the Exchange rates page*
- *Rates as of 31 Oct 2026 · entered by hand · last change 2 Nov 2026 by Sam*
- any of these, then *· SEK not converted*

"As of" is the rate's own date; "fetched" is when someone last fetched. With
several currencies on a page the oldest of their newest rates sets the date;
the **?** beside it lists each one. Stale is a flag, never a block: the
figures are still converted, and the line says how old their rates are. A
report showing a single currency has nothing converted and no line.

The **Exchange rates** page is also the sync dashboard: the last fetch, the
coverage of each currency's transaction dates, gaps, a month-by-month strip,
the fetch history (kept 90 days), the stored rates beside your own, and the
currencies no source covers -- including an account in a code ISO 4217 never
assigned, which no source will ever publish.

## Changing the choice changes past figures

Changing the chain, the policy or the base currency changes every converted
figure at the next load, and so does a manual rate entered for a past date.
The page asks before saving such a change, and History records it.

## For programs

The agent API reads rates and their age and changes nothing about them:
`GET /api/agent/v1/households/{id}/fx/rates` and `…/fx/status`. A key cannot
fetch, enter or delete rates, or change the chain. `agent/README.md` has the
conventions.

## Currency codes and decimals

Amounts are integer minor units at each currency's ISO 4217 exponent. Since
0.14, forint (HUF) and New Taiwan dollar (TWD) amounts have two decimals, as
ISO 4217 gives them; the upgrade multiplies every stored HUF and TWD figure by
100, so each keeps its meaning.
