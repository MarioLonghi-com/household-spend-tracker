---
id: payee-rules
title: Name and categorise payees automatically
screens: [rules, payee-categorisation, payees]
also:
  - payee rules
  - rename what the bank calls a shop
  - one shop appears under many names
  - always put a payee in the same category
  - merge two payees
  - stop guessing the category for a payee
---

Each imported line goes through two steps. A rule on **Payee Naming Rules** turns the bank's words into a payee, and then that payee's setting on **Payee Categorisation** gives the transaction its category. **Payee Merge** folds two spellings of one payee together. All three are under **Payee** in your household's menu.

## Write a naming rule

1. Open [Payee Naming Rules](screen:rules) and press **Add a rule**.
2. Under **And then**, choose **call it one payee** for a shop, or **take this bit off, and see what is left** for a payment service whose words come before many different shops.
3. Under **When the statement line**, choose **contains**, **starts with**, **is exactly** or **matches the pattern** (a regular expression), and type the bank's words in **This text**.
4. For a shop, type its name in **Call the payee**. To strip text, usually leave **Put this back instead (leave empty to remove it)** empty.
5. Press **What would this match?** to see how many rows already in the ledger it would claim, with examples.
6. Press **Create**.

A rule matches *the bank's own words*, not the payee the register shows. Open a transaction and press **Where did this come from?** to see exactly what arrived. Case does not matter. Rules that strip text run first, each working on what the last one left; then the first naming rule that matches wins. **Order (lower runs first)** sets the order. Untick a rule's **On** box to pause it; **Delete** removes it at once.

## Apply rules to rows already here

Rules run as a statement is read, so a new rule changes nothing already in the register. Right after **Create**, the panel **Apply rules to what is already here** opens: it shows how many rows would move and to which payee before you confirm. **Only rows nobody has corrected** leaves alone a payee somebody set by hand. It is one entry in [History](screen:history), so one undo puts every row back.

When the bank sends one shop with a different reference each time, a card headed **These look like the same shop** appears. **Write this rule** starts a rule with the text filled in.

## Choose a payee's category

1. Open [Payee Categorisation](screen:payee-categorisation). The **Categorised as** column shows what each payee has been filed under so far.
2. Press **Categorisation** on the payee's row.
3. Choose **Auto-categorise based on payee history** (the most common category of its last three transactions), **Always use my choice** and a category, or *Do not auto-categorise*, which leaves new transactions blank.
4. Press **Save**.

This decides the category of the payee's new transactions. An archived category cannot be the fixed choice.

## Merge two spellings

1. Open [Payee Merge](screen:payees) and use **Find a payee**.
2. Press **Merge…** on the spelling you want to fold away.
3. In **Into**, choose the payee to keep, and press **Merge**.

Every transaction and every rule that named the first payee now names the one you kept, and the first is gone. Undo it from History. Payees that stand for a transfer cannot be merged. Payees that differ only in accents, dashes or invisible spaces are listed under **Spellings of one name**; press **Review…** and choose the one to **Keep**.

To set up categories themselves, see [Organise categories and groups](help:categories); for the import itself, see [Import a statement](help:import-statement).
