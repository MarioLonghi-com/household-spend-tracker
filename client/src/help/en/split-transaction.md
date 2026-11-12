---
id: split-transaction
title: Split a transaction
screens: [register]
also:
  - two categories for one purchase
  - divide a payment between categories
  - one shop bill with groceries and other things
  - split a receipt
  - undo a split
---

When one payment covers several things, such as food and a present bought at the same shop, split it into parts, each with its own category and memo. The parts replace the original, and every part is an ordinary transaction.

## Split it

1. In [Transactions](screen:register), click the row to open its panel and choose **Split**.
2. The panel opens with two equal parts. The first keeps the original's category.
3. Type each part's amount and choose its **Category**. A **Memo** is optional: a part without one keeps the original's.
4. Use **Add a part** for a third, fourth or fifth part, and **Remove** to drop one. **Split equally** spreads the amount evenly again.
5. When the box at the top says **It adds up**, save with the button below the parts, *Split into 2* (or however many parts there are).

With two or three parts there is also a bar you can drag to divide the amount. Typing an amount in one part moves its neighbour, so the total stays right.

## The rules

- The parts must add up exactly to the original. Until they do, the panel says how much is left to account for, and the save button stays off.
- Between two and five parts, and none of them zero.
- No minus signs: every part goes the same way as the original, money out or money in.
- The parts keep the original's date, payee and cleared state, and any receipt is attached to every part.
- A leg of a [transfer](help:transfers) cannot be split, and neither can a locked (reconciled) transaction. The **Split** button does not appear on those.
- A work expense stays a work expense in every part. A payment that repaid work expenses cannot be split until they are taken off it: see [Record money someone owes you back](help:reimbursements).

## Afterwards

Each part is in the register on the same date, and its panel says it is one part of a split. You can change or delete one part on its own; the others stay.

Closing the panel before saving changes nothing. If you have already typed something, it asks first: **Keep editing** or **Discard**.

## Undo a split

The split is one act in [History](screen:history). Open it there and choose **Undo this**: the parts go and the original comes back exactly as it was.
