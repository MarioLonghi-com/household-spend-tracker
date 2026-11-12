---
id: one-time-import
title: Bring your history across from another app (YNAB)
screens: [household, import]
also:
  - import from YNAB
  - move from another budgeting app
  - bring my whole history in at once
  - YNAB personal access token
  - Register.csv from YNAB
  - undo the YNAB import
---

Importing statements is one file per account, month after month. The **One-time Import** is for the day you move here from another budgeting app: it brings that app's whole history in once (transactions, accounts, categories and transfers) instead of statement by statement. YNAB is the one workflow so far.

## Run it

It lives on the [household's own page](screen:household), in the **One-time Import** section, and the household's owner runs it.

1. Press **Start a one-time import**.
2. Choose how to connect. With **Export file**, upload YNAB's export: the zip as YNAB gives it, or the `Register.csv` inside it. The `Plan.csv` is not needed and is ignored. With **API key**, paste a YNAB personal access token and pick the plan. Both ways are offered on every deployment.
3. Map the accounts and categories, then go through the options.
4. Press **Preview**. It runs the whole import first and throws it away, so you see what would happen.
5. Press **Import**, and read the report.

The token is used for that one visit and never stored: it is not kept in the ledger, in History or in any log, and closing the import forgets it.

## You map, it suggests

Each YNAB account goes to an account you already have, to a new one (**Create new…**), or is skipped. Each YNAB category goes to one of yours, to a new one (**Create new…**), or arrives uncategorised. Suggestions come from the names, and an account is only suggested in the same currency.

## What comes across

- *The bank's own words, through the API.* For a row YNAB imported from a bank, its API still has what the bank wrote, and that is kept beside YNAB's payee name; the payee is the name you kept in YNAB. The report then points at **Payee Naming Rules**, which can suggest rules from that text before your first statement arrives, and says how many groups of bank strings now look like one payee each. The `Register.csv` has only the clean names, so a file import keeps no bank text, and its report says rules can be suggested after the first statement import.
- *Cleared states are reset.* YNAB's reconciled and cleared marks do not carry over: every row arrives uncleared, to be reconciled here against the bank. The import asks you to confirm that before it commits.
- *Transfers are linked.* When both sides of a YNAB transfer are imported they become one linked transfer here. When one side is skipped, out of the date range or missing, the other arrives as an ordinary row and the report says why.

## Duplicates

A row that looks like one already in an account you mapped to (same amount, dated up to 3 days apart) is shown before anything is written, and skipped unless you say to import it, one by one or all at once. A row an earlier one-time import already brought in is always skipped, so running it again only adds what is new. A file's row is known by what it says (account, date, amounts, payee, category and memo), not by its line, so a fresh export with rows added above still skips everything imported before.

## Statements afterwards

A statement imported later for the same months matches each line to the row the One-time Import brought in (same amount, dated up to 4 days away) and marks it as seen by the bank instead of adding it again. For a row YNAB had itself imported from the bank, the match is exact: it is that bank line, even if you moved its date in YNAB. A row you have reconciled here is left alone. Running the One-time Import again still skips a row a statement has matched. See [How import reads a statement](help:import-how-it-works).

## Partial, not all-or-none

A row that cannot be read (a bad date, an amount that does not fit the currency) is left out and the rest still go in. The report after the commit counts every row: imported, linked, skipped and why, failed and why. What did not go in downloads as a `.txt` file.

## One undo reverses it

The whole import is a single entry in [History](screen:history), *One-time Import · YNAB (CSV)* or *One-time Import · YNAB (API)*. Undoing it takes back every row, transfer link, account, category and payee it created, and like the import itself it is the owner's to do. Each row it brought in says so in the register's **Where did this come from?**, with the file or plan and the date.

## Another app?

More workflows can be added to the One-time Import. Suggest one on [GitHub](https://github.com/MarioLonghi-com/household-spend-tracker/issues/new).
