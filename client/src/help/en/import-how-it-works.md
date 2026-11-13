---
id: import-how-it-works
title: How import reads a statement
screens: [import]
also:
  - which file formats can I import
  - why was a line not imported
  - what does already imported mean
  - pending transactions missing from import
  - running balance does not add up
  - how the category of an imported line is chosen
---

What happens to a statement from the moment you choose the file to the moment its rows are in the register, and every way a line can go. To do it, see [Import a statement](help:import-statement).

## Getting the file in

- Upload a file, or paste the rows if your bank only lets you copy them from a web page. Both go through the same path.
- Formats read: CSV and other delimited text (comma, semicolon, tab or bar); OFX, both the old SGML shape (1.x) and XML (2.x); old Excel `.xls`; and PDF statements whose layout the app knows.
- Not read: `.xlsx` (save it as CSV or `.xls`), and camt.053 and MT940. Those are understood, but no real sample file has been seen here, and a format built from its specification alone is one that has never been tested against a real file.
- A file far larger than a statement is refused, with a sentence saying which limit it went over: a PDF of more than 50 pages, or one that unpacks to far more text and drawing than a statement holds; an `.xls` whose first sheet runs past a million cells; a single cell longer than 128 KB. Only the first sheet of an `.xls` is read.
- One account per import, and you choose it. If the file says which account it is for (the account number an OFX file states, an identifier in the file's name, or an IBAN in its opening lines) the account is chosen for you and the screen says why. If the file names a different account from the one you picked, you are told. If it names none, and its file name carries one code that could be the account's tag, the screen offers to keep that code as the tag of the account you pick, only if you say **Add**.
- The same file twice is refused while the first is waiting or imported, with a way to open the waiting one. Once an import is undone, its file can go in again.
- A savings statement dropped on the wrong pocket is refused when every row names the other account's pocket.
- Agents holding a key can stage rows through the same checks. History shows the person whose key it was, and the agent that used it.

## How the file is read

Nothing is typed in. Every guess below is shown at the top of the preview, so a wrong one is visible before anything is written.

- *Where the table starts.* A title block above it (account number, holder, balance) is stepped over. The encoding is worked out, so accented names, a Windows export's € and curly quotes, and a UTF-16 "Unicode text" file all arrive intact. A UTF-16 file without its byte-order mark is refused with a request to save it again as UTF-8, rather than guessed at.
- *The date.* A column named for a date in any of several languages, or abbreviated the way Spanish banks do (`F. Valor`, `F. Operación`). Day-first or month-first is decided from the whole file, and you are warned when nothing in it settles the question. Dates written with a time keep only the date. When a file has both a started and a *completed* date, the completed one is used: it is when the bank booked it, the date the running balance follows, and the date the other side of a transfer carries.
- *The amount.* Either one signed column, or separate money-in and money-out columns. Currency symbols, thousands separators and parentheses for negatives are all read, and so is a minus written at the end (`12,50-`) or as a typographic minus or dash, and a debit or credit written after the figure (`12.50 DR` is money out, `12.50 CR` money in). An amount signed twice (`-12.50 DR`), or one that is not a number at all, is refused with the reason rather than read as zero. Whether a comma or a point is the decimal one is worked out from the amounts and the balance. When every one of them reads either way (`1.500` is fifteen hundred in Spain and one and a half in Britain), the account's country decides, or a point when it has none, and you are warned that it was assumed. If every amount in the file is positive and nothing says which way they go, you are warned: importing it as it is would read a month of spending as income.
- *Never the amount:* a balance, a rate (AER, NIR) or a date column, even when its name looks like one.
- A fee column becomes a row of its own under *Bank fees*, next to the payment it was charged on. The payment itself is not changed.
- A running balance is read and checked (see the next section). It is never taken as the amount.
- Everything the bank wrote is kept with each line, under the bank's own column names, so a transaction can be explained after the file is gone.

## Checks on the file as a whole

- *A file holding several accounts.* Some banks put more than one account in one file and tell them apart with a Product column. Revolut's account statement holds the current account (`Current`) and every savings pocket (`Deposit`). Each account takes one product: a current account takes `Current` unless you set its **Statement product** on the [Accounts](screen:accounts) screen. The other rows are listed as **Not for this account**. If an account has not said which product it takes and no safe guess exists, the file is refused with instructions.
- *A file in another currency.* A Currency column (or Moneda, Divisa, CCY) and an OFX file's own currency are checked against the account's. Every amount is recorded in the account's currency, so a row in another one is never imported. In a table that also holds rows in the account's currency it is listed as **Not for this account**; otherwise, and always in an OFX file (which is one account), as **Could not read**, with the bank's rate if it gave one. Nothing is converted. Either way the reason names both currencies. A file whose every row says it is in another currency is refused. An agent's row that names its currency is held to the same check, and one in another currency is **Could not read**. A column saying what a purchase cost before it was converted (Original, Moneda origen, Local, Transaction currency) does not count: that amount is already in the account's money. If two columns could each be the currency the amounts are in and neither is named for the account, the bill or the settlement, neither is used and the preview says so.
- *Does the running balance add up?* Each row's balance should be the one before it plus what the row moved. If it breaks, the preview says where: a row is missing, or rows from another account are mixed in. If a figure in the file is too large to record as money, that line is rejected and the preview says the balance was not checked.
- *Does it carry on from the ledger?* The balance before the file's first row should match what the register holds for the day before. If it doesn't, the preview gives the difference: a statement between your last import and this one is probably missing.
- A payee rule that claims most of a file, or one too short to be safe, is pointed out on the lines it claimed.

## What happens to each line

The preview's **What happens** column gives each line one of these:

- **New** (`created`): becomes a new transaction when you commit. If the line is one side of a transfer the importer is sure of, it is linked to the other side as well.
- **Already have it** (`matched_existing`): you typed this one in yourself, or a One-time Import brought it in: same amount, dated up to 4 days away, or, for a row YNAB had imported from the bank, that very bank line, whatever date it now has. Your row is kept and marked as seen by the bank instead of a second one being added. You can reject the match on the preview with **Not the same thing — add it separately**.
- **Already imported** (`duplicate_skipped`): this line is already in the account, from this file or an earlier statement that overlaps it. Nothing is added.
- **Needs a look** (`needs_review`): the row it matched changed between the preview and the commit, or was already matched to another statement line. It was left alone and not imported.
- **Could not read** (`rejected`): the line has no date or amount that could be read, its amount, fee or balance is too large to record as money, it says it is in a currency other than the account's, or the bank says it did not happen or has not settled yet (see below). One bad line costs one line, never the file.
- **Not for this account** (`skipped`): read fine and deliberately not imported. It belongs to another account in a file that holds several (another product, or another currency), or it moves no money at all.

### Rows the bank says did not happen, or have not happened yet

A state column saying `REVERTED`, `DECLINED`, `FAILED`, `CANCELLED` (or `CANCELED`), `REJECTED` or `ANULADO` means no money moved, and the line is refused with the bank's own word as the reason. `PENDING`, `PENDIENTE` or `HOLD` means it has not settled: it is refused this time and imports from the next statement once it has settled, so a tip or an exchange rate added on settlement cannot create a second copy.

### Lines that move no money

A `0.00` line, such as a day's interest that rounded to nothing or a "Closing transaction", is shown and not imported.

### How "already imported" is decided

By the bank's own transaction id when the file has one (OFX `FITID`). Otherwise by the amount, the date and the running balance after the line, which stays the same whatever dates your download covered. Without a balance, it is the amount, the date and the line's position among identical ones that day. Statements that overlap therefore import only what is new. Lines imported by an older version of the app are still recognised.

A line counts as already imported only while its row is in the register. Delete the row and the line is new again: the next statement that carries it, or the same file sent again with **Import it anyway**, brings it back, known by the same identity. The statement is the record. Undo in [History](screen:history) is the other way back, and it also restores anything you had changed on the row.

### Payee and category

Your payee naming rules decide the payee, and the payee's categorisation decides the category (see [payee rules](help:payee-rules)). Names and rules are compared ignoring capitals, accents, the kind of dash and spaces you cannot see, so *Café Sol* and *CAFE SOL* are one payee; a regular-expression rule is tried against the bank's own words as well as that plainer form. A category you choose on the preview wins over both. When neither says anything, the bank's fixed wording decides for a few kinds of line: interest goes to the *Interest* payee and *Interest income*, Revolut's investment account and robo portfolio to *Investments*, and fees to *Bank fees*. A category the importer needs and you don't have yet is created when you commit, and removed again if the import is undone. What the bank wrote is never overwritten.

## Staged, then committed, then undoable

- Nothing reaches the register until you commit. A read file is staged and waits in the queue on the Import screen, under **Waiting to be reviewed**, where it can be opened again or thrown away.
- Every time a staged import is opened it is checked again against the register as it is now. A duplicate whose original has since been undone becomes new again, and the preview says how many lines changed. Commit checks once more, so a tab left open cannot import a line twice.
- One import is one act in History, and undoing it removes every row it added, every transfer link it made and every category it created. Reconciled rows are never matched as **Already have it**.

Transfers between your own accounts have rules of their own: see [how imported transfers are matched](help:import-transfers). How a file finds its account is in [account identifiers](help:account-identifiers).
