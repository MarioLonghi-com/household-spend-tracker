---
id: import-transfers
title: How imported transfers are matched
screens: [import, transfers]
also:
  - why was my transfer not linked
  - import links the wrong transfer
  - transfer waiting for the other statement
  - money to savings counted as spending
  - suggested transfer on import
---

Money moving between two of your accounts appears in both banks' statements, often imported days apart. Linked as one transfer, the two rows stay out of Income vs Expense. This is how an import decides. For linking by hand, see [Move money between your own accounts](help:transfers).

## A candidate pair

Two rows on different accounts, same currency, exactly opposite amounts, dated at most 5 days apart.

A row with a category is not a transfer. It is never paired, never linked on commit and never waiting, and categorising happens before matching, so a row whose payee rule or history gives it a category is left alone. Clear the category and it is matched again.

## Linked on commit without asking

A pair is linked as the import is committed when both of these hold:

- one row names the other account (its number, IBAN, card or a name the bank uses for it; see [account identifiers](help:account-identifiers)), or these two accounts have had transfers linked before, by a row that named the other side or by you. A link made on that history alone does not count;
- *and* no other row could be its pair, or this one is strictly the closest in date, or, when date cannot tell two of them apart, its two descriptions share strictly more words than any rival's, not counting words like *TO*, *FROM* or your account numbers.

A link that makes two accounts' history makes their other pairs strong too, so those are linked in the same commit, and by *Link all* on the [Transfers](screen:transfers) screen, which links until nothing new is strong.

## Suggested instead

- When only the amounts and dates match, or when a row could pair with more than one, the pair is suggested. The preview says so, and the Transfers screen is where you confirm it.
- Never linked without asking, however well it matches: money out of a credit card (a purchase, and its refund or cashback is not a transfer), and a row whose payee has a category rule or whose words name someone who has paid you before, an employer's expense refund, say. These are suggested instead.

## Pairs you have turned down

A pair you have said is not a transfer, by unlinking it or with **Not a transfer** on the Transfers screen, is never matched again. Undoing that in [History](screen:history), or linking the pair yourself, puts it back.

## Waiting for the other statement

When a row names one of your accounts or a household member and its other side has not been imported yet, it waits. It is linked when that statement arrives.

## Two currencies

Two currencies are never matched automatically, because the amounts differ by a rate nobody printed. Select both rows in the [register](screen:register) and choose **Link as transfer**; the rate is worked out and kept.
