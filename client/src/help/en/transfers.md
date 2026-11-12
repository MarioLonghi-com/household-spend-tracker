---
id: transfers
title: Move money between your own accounts
screens: [transfers, register]
also:
  - record a transfer
  - link two rows as a transfer
  - transfer between currencies
  - money moved to savings shows as spending
  - unlink a transfer
  - not a transfer
---

A transfer is money moving between two of the household's own accounts. It is one act made of two rows, one in each account, and it has no category: it is not spending and not income, so it stays out of [Income vs Expense](screen:report-income-expense).

## Record a transfer by hand

1. Open [Transactions](screen:register) and press **Add transaction**, then choose **Transfer**.
2. Pick the account the money leaves in **From** and the one it arrives in under **To**.
3. Set the **Date**, type the amount leaving without a minus sign, and add a **Memo** if you like.
4. Press **Transfer**.

Both rows are written together, each with *Transfer :* and the other account's name as its payee. Changing the amount on one side changes the other side too, and undoing the act in [History](screen:history) takes both back. The form needs two open accounts.

## Between two currencies

When the two accounts are in different currencies, the form asks for the amount arriving as well. Type the figure your bank actually credited: the app never invents a rate. It shows the rate the two amounts work out at, and keeps that rate on the pair.

## Link two rows you already have

When each bank's statement brought in its own side, make the two rows one transfer:

1. In [Transactions](screen:register), tick the money out in one account and the money in in the other.
2. Press **Link as transfer** in the bar that appears.

The two must be in different accounts and go opposite ways. In one currency the amounts must match exactly; across currencies they may differ, and the rate between them is worked out and kept. Each row keeps its own date and bank description, and loses its category. A row that is part of a split or a work expense cannot be linked. To undo a link, tick one side and press **Unlink transfer**.

## The Transfers screen

Imports link the pairs they are sure of as they go (see [how imports match transfers](help:import-transfers)). The [Transfers](screen:transfers) screen holds the rest, in the same currency only:

- **Sure of these**: one row names the other account, or the two accounts have been linked before. *Link all* links every pair in the section.
- **Worth a look**: the amounts match and the dates are close, but nothing names the other account. Press **Link** on a real transfer, or **Not a transfer** so the pair is never offered again. Tick several and press **Link selected** to link them in one go.
- **Waiting for the other statement**: rows that name one of your accounts whose other side is not in the ledger yet. They are linked when that statement is imported. If one is not a transfer, give it a category with **Not a transfer — categorise as…**.
- **Linked by history only**: links no person chose. Press **Unlink** if one is wrong, or **Keep** to confirm it.
