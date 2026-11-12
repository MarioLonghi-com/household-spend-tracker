---
id: backups
title: Back up your ledger and restore it
screens: [application]
also:
  - make a backup
  - download a backup
  - save a backup to google drive or dropbox
  - restore a backup
  - move the ledger to a new computer
  - what is secret.key
---

A backup is a complete copy of the ledger: every household on this installation, receipts included. Backups are made on the [Application management](screen:application) screen, which only an owner can open.

## Make a backup

1. Open **Application management** from the **Admin** menu.
2. Under **Database**, in **Back the database up**, press **Back up now**.

The copy is checked and written into the backups directory while the app keeps running, and it appears in the list below the button. **Last backup** says when the newest one was made. Nothing here deletes a backup on its own; **Delete** removes one when you confirm, and that cannot be undone.

## Take a copy off this machine

A backup that stays beside the ledger does not help if the machine is lost. On a backup's row:

- **Download** gives you a zip.
- **Save to Drive/Dropbox…** offers the ways this browser can put the zip into a cloud folder: your device's share sheet, **Choose a folder…** for a synced folder, or a download to upload yourself. The page never talks to Google or Dropbox.

The zip holds the ledger (`spendtracker.sqlite3`), a `manifest.json` that lets a restore check it, and a `README.txt` that tells whoever opens it later what it is and how to put it back.

## The secret key

Authenticator codes are sealed with a key, `secret.key`, which a backup does not carry unless you ask. Tick the box above the list to include it; each download then asks for your password and a code. Keep such a zip somewhere only you can open: with it and a member's password, anyone can sign in as that member.

Without the key, the zip restores onto this same installation as it is. Restored anywhere else, every authenticator is refused unless the key is supplied separately; recovery codes still work.

## Restore a backup

On a server there is no restore button. Whoever runs the installation stops the app and runs `make restore FROM=` with the zip or backup folder, adding `KEY=` with the path to `secret.key` when the zip lacks it and goes to another installation. The ledger in place is moved aside, not overwritten. Passkeys only work under the host name they were made for, so after restoring onto another name members sign in with password and code and add their passkeys again.

Each update also takes its own backup, listed under **Backups taken by updates**; the newest five are kept so the update can be undone from **Updates**.

Never back up by copying the database file: recent changes sit in a second file beside it, and a copy of one file alone can be empty.

## On the desktop app

The desktop app has no authenticators, so there is no key to include. Its **File** menu has **Back up now**, **Restore from backup…**, **Choose backups folder…** and **Show backups folder**. It also backs up on the first launch of each day and when you quit after a change. A restore backs up the current ledger first, then asks you to sign in again; it refuses a backup made by a newer version. Restoring is also how you move the ledger to a new computer.
