---
id: exchange-rates
title: Keep exchange rates up to date
screens: [exchange-rates, report-net-worth, report-income-expense]
also:
  - fetch exchange rates
  - enter a rate by hand
  - reports say rates are stale
  - currency not converted
  - which rate does a report use
  - import rates from a csv file
---

The ledger never converts: every amount stays in its account's currency. Exchange rates are only for reports, which can show figures in the household's base currency. Each converted figure is marked ≈, and the *Rates as of* line on the report says how old the rates are. Any member can look after them on the [Exchange rates](screen:exchange-rates) page.

## Fetch the rates

Nothing is fetched on its own.

1. Open **Exchange rates**. The line under **Fetch now** says which currencies would be fetched, from where, and from which date.
2. Press **Fetch now**. It fetches only the dates not stored yet, and carries on if you leave the page.
3. To fetch some dates again, press **Refetch a range…**, choose **From** and **To**, press **Refetch** and confirm. Fetched rates for those dates replace the stored ones; rates entered by hand are not touched.

**Fetch history** lists every fetch, who started it and what it added.

## Enter a rate by hand

For a currency no source covers, or a rate you want to set yourself:

1. Under **Rates entered by hand**, type the **Currency** code. You can also press **Enter a rate** on its row under **Per currency**.
2. In **For**, choose **A day** or **A month**. A monthly rate covers every day of its month.
3. Pick the date or month, and type the rate as units of that currency per 1 of the base currency, the way the ECB quotes it.
4. Press **Add the rate**, or **Replace the rate** if one is already stored for that date.

For many rates at once, choose a CSV file with the columns `currency,date,rate` in **Rates file**, check the preview and press **Save these rates**. It is all or none: one line with a problem keeps the whole file out. A rate entered by hand can be deleted under **Stored**, and [History](screen:history) can bring it back.

## Settings

- *Rate sources, in order.* The first source with a rate within the stale limit is used. By default rates entered by hand come first, then the ECB. Use **Up**, **Down** and **Remove** to change the order, or add *your own transfers*: the rates your own currency transfers implied, fees included, on the days you exchanged money.
- **Treat a rate as stale after (days)**: seven by default. A stale rate is still used, and the report says so.
- **Rate used for monthly figures**: **Monthly average**, **Month-end rate** or **Daily rate per row**. This is what [Income vs Expense](screen:report-income-expense) converts with when it shows every currency in the base currency. [Net worth](screen:report-net-worth) always converts each balance at the rate of its own date.

Press **Save settings**. Changing the sources or the monthly rate changes past converted figures, so it asks first; press **Change it** to go ahead. The change is written to History.

## See what is stored

**Per currency** shows each currency's source, first and last rate, coverage and gaps. **Months** shows a strip of which months have rates, and **Browse rates** lists the stored rates by day or by month, with the figure a report would use. A currency under **Not covered** has no source; enter its rates by hand. The base currency itself is set on the [household page](screen:household).
