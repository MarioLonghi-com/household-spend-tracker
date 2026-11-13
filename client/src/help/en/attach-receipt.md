---
id: attach-receipt
title: Attach a receipt to a transaction
screens: [receipts, register]
also:
  - add a photo of a receipt
  - upload an invoice PDF
  - match a receipt to a payment
  - where are my receipts stored
  - keep the original photo
  - receipts waiting in the inbox
---

A receipt is a photo, scan or PDF kept on a transaction as evidence. You can attach it straight to the transaction, or put it in the receipts inbox first and match it when the statement arrives. Both are normal ways round.

## On a transaction

1. Open [Transactions](screen:register) and click the row to open its panel.
2. In the **Receipt** section, choose **Attach receipts** and pick one or more files. You can also drag files onto the button, or paste a screenshot while the panel is open.
3. Wait for each file to finish. The server compresses it, which can take a few seconds for a phone photo.

Once a transaction has a receipt, the panel offers **Add more** for another one, **Replace** to swap the one on screen, and **Detach** to take it off. Neither replacing nor detaching deletes anything: the receipt goes back to the inbox. In the register, a paperclip marks the rows that have a receipt.

## From the Receipts screen

[Receipts](screen:receipts) is the inbox: everything that is not on a transaction yet. Photos sent from [your phone](help:snap-receipt) land here.

1. Choose **Add receipts** to upload files without a transaction, or click one that is already waiting.
2. In its panel, choose **Attach to a transaction**.
3. The list opens on the ten days either side of when the photo was taken, or when it was uploaded if the file carries no date. Find the row and choose **Attach**.

**Inbox**, **Matched** and **All** switch between what is waiting, what is already attached, and everything. **Receipt notes** is a free line for what it was for; in the **List** view you can type it straight into the table.

## Where receipts are stored

Receipts are kept in the household's own database on this server and nowhere else. What is kept is chosen on the household page (click the household's name in the menu), under **Receipts**:

- *Readable copies only*, the recommended choice: a screen copy at most 2000 pixels on its long side and a thumbnail, both compressed to AVIF. About 33 KB per receipt.
- **Also keep the original file**: the exact file from your camera or scanner as well. About 2 MB per receipt, roughly 70 times the space.

A PDF is always kept exactly as uploaded, whatever the choice, with a picture of its first page to show on screen. The choice applies to new receipts only: nothing already stored is converted or deleted. If whoever runs the server has turned originals on for every household, the page says so and the choice cannot be changed.

**Download** gives you the original when it was kept, and the screen copy otherwise. **More info** shows when and where a photo was taken, read from the photo itself.

## Good to know

- Accepted: JPEG, PNG, HEIC, WebP, AVIF, GIF, TIFF, BMP and PDF, up to 25 MB each.
- The same file twice on one transaction, or twice in the inbox, is refused. The same file on two different transactions is allowed, for a bill that covers two payments, and the panel says it is also on the other one.
- When you [split a transaction](help:split-transaction), its receipts go onto every part.
- Deleting a receipt on the Receipts screen can be undone from [History](screen:history).
