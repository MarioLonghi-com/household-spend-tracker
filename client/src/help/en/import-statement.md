---
id: import-statement
title: Import a statement
screens: [import, register]
also:
  - upload a bank statement
  - bring in my bank transactions
  - paste rows from my bank's website
  - import a CSV or OFX file
  - finish an import I left halfway
  - undo an import
---

A statement goes in one account at a time. Nothing reaches the register until you say so, and the whole import can be undone in one go.

## Import a file

1. Open [Import](screen:import).
2. Under **Into which account**, pick the account the statement belongs to. If the file says which account it is for, the app picks it for you and says why; if the file names a different account from the one you picked, it tells you.
3. Choose the file under **File**: a CSV, an OFX or QFX download, a bank's .xls export or a statement PDF. If your bank only lets you copy rows from its website, paste them into the box underneath instead.
4. Press **Read the file**. The preview, **What this would do**, shows what was detected in the file and every line with what will happen to it.
5. Look over the lines. Click a line's category to change it; type any part of a category's name, or empty the box to go back to the suggestion. If other lines have the same payee, you are offered to give them the same category, and with **Yes, all of them and set the rule** to keep doing so for future statements. **No, just this one** changes only that line.
6. Untick a line in the **Add** column to leave it out.
7. When a line is **Already have it** but is really a different transaction, press **Not the same thing — add it separately**. **Undo — add as new instead** takes that back.
8. Press **Import these**. **Cancel** closes the preview without writing anything.

If the file names no account but its file name carries a code that could be the account's tag, the screen offers to keep it: press **Add** to keep it, or **Ignore**. See [account identifiers](help:account-identifiers).

## Imports left halfway

A file you read but never imported waits under **Waiting to be reviewed**. Press **Open** to carry on where you left it, or **Discard** to throw it away; nothing in it has reached the register. Reading the same file again is refused while one is waiting, with **Open that import** to go to it, **Import it anyway** and **Discard the staged one**.

## Undo an import

One import is one entry in [History](screen:history). Undoing it there removes every row it added, every transfer link it made and every category it created.

## More

- [How import reads a statement](help:import-how-it-works): formats, checks, and what each outcome means.
- [How imported transfers are matched](help:import-transfers).
- Moving from YNAB? Bring your whole history in once with the [One-time Import](help:one-time-import).
