---
id: account-identifiers
title: Tell the app what your bank calls an account
screens: [accounts, import]
also:
  - statement picks the wrong account
  - add an IBAN to an account
  - account identifiers
  - transfers between my accounts are not linked
  - how banks write our names
  - suggested identifiers
---

Identifiers are the numbers and names banks use for your accounts. They are how a statement file finds its account on the Import screen, and how a transfer finds its other side.

## Add one

1. Open [Accounts](screen:accounts) and press **Settings** on the account.
2. Under **What banks call it**, choose the **Kind** and type the **Value**.
3. Press **Add**.

A household member's name goes in **How banks write our names** on the Accounts screen itself, because it belongs to no account. If an account's own name contains its IBAN or a long number, the app offers to add it for you.

## The kinds

- `iban`, `number`, `card`: matched with spaces ignored. Short numbers (under six characters) only match as a whole word.
- `alias`: a name the bank uses for the account, such as a savings pocket's name. Matched as whole words.
- `file_tag`: the code a bank puts in its download's file name. Only used to recognise files.
- `holder`: how a bank writes a household member's name. It belongs to no account: it means "this is our own money moving". Banks write one person's name many ways, so a name of two words or more matches in any order, with initials for some of its words, and with the last word of the text cut short (four letters or more). A name of three words or more also matches when two of them stand together and one is four letters or longer. A one-word name only matches as that whole word. A name only ever makes a transfer *suggested*, never linked on its own; the Accounts screen shows what each name matches.

## Suggested identifiers

The Accounts screen also lists, under **Suggested identifiers**, identifiers your register already uses and nobody has added: words on the other side of transfers you have linked, IBANs and card numbers that pass their check digits, `A/C` and sort-code numbers, quoted pocket names, an account's own name, and the code every statement file of an account carries. Each says how many rows mention it (**Mentions**) and how many transfer pairs adding it would link (**Would link**).

None is added by itself. An identifier lets transfers link without asking, so a wrong one makes wrong links: each waits for **Add** or **Ignore**, and an ignored one is not offered again.

See [how imported transfers are matched](help:import-transfers) for what an identifier changes, and [How import reads a statement](help:import-how-it-works) for how a file is matched to its account.
