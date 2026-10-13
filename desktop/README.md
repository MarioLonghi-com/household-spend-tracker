# The desktop app

Spend Tracker for one household on one computer: the same backend and the same
client as the server, in an Electron window, for the Mac App Store (Apple
Silicon) and the Microsoft Store (Windows 11 x64). Epic #49; this folder is
#61 and #62.

## How it fits together

```
 Electron window  ──  app://localhost/…  ──►  shell (src/main.ts)
                                                │  every request, as frames
                                                ▼  over stdin / stdout
                                       frozen backend (PyInstaller --onedir)
                                       app/desktop/stdio_adapter.py → FastAPI
```

- **No port is opened.** The shell registers `app://localhost` and forwards
  every request, the client's own files included, to the backend over its
  standard input and output (`src/scheme.ts`, `src/backend.ts`). FastAPI's
  headers, CSP and CSRF check apply exactly as on a server.
- **One profile setting decides what is offered** (`SPENDTRACKER_PROFILE=desktop`,
  `app/capabilities.py`): no invitations, agents, passkeys, authenticator or
  self-update; a password at every launch and one recovery code (#320). No
  screen asks whether it is on a desktop.
- **The session lives in the shell's memory** (`src/cookies.ts`): Chromium
  keeps no cookie from `protocol.handle`, and a session that ends with the app
  is what "a password at every launch" asks for.

## Where things are

| | macOS (Mac App Store) | Windows 11 (MSIX) |
|---|---|---|
| The app | `/Applications`, read-only | `C:\Program Files\WindowsApps\…`, read-only |
| The ledger, its logs, upgrade backups | the sandbox container's `Application Support/Spend Tracker/Ledger` | `%LOCALAPPDATA%\Spend Tracker\Ledger` |
| The shell's settings and log | `…/Spend Tracker/shell.json`, `shell.log` | `%LOCALAPPDATA%\Spend Tracker\shell.json`, `shell.log` |
| Backups | a folder the person chooses; until then `…/Spend Tracker/Backups` | `Documents\Spend Tracker Backups`, or the folder chosen |

Both data folders survive an uninstall. On Windows that is because the folder
is excluded from MSIX virtualisation in `appxmanifest.xml` (proved on Windows
11 in #59).

The **File** menu has *Back up now*, *Restore from backup…*, *Choose backups
folder…*, *Show backups folder* and *Show ledger folder*. A backup is a bare
`.sqlite3`, whole on its own; one is also made on the first launch of each day
and on quit when anything changed. The folder keeps every backup from the
newest day, the newest of each of the last 30 days that have one, and the
newest of each of the last 12 months (`src/retention.ts`). Restore is also how
a ledger moves to a new computer.

## Building

PyInstaller freezes the Python it runs in, so each OS builds its own: the Mac
builds on an Apple Silicon Mac, the Windows one on Windows x64.

```bash
python3 -m venv build/freeze-venv
build/freeze-venv/bin/pip install --require-hashes -r requirements-desktop.txt
(cd client && npm ci) && (cd desktop && npm ci)
build/freeze-venv/bin/python -m scripts.desktop_build mac     # or mas, or win
```

`scripts/desktop_build.py` builds the client, stamps the commit, freezes the
backend (`desktop/backend.spec`), smoke-tests it (`smoke_backend.py`), builds
the shell and packages it into `desktop/dist`. `--from N` starts at step N.

### Checks

```bash
cd desktop && npm test          # the shell's units
cd desktop && npm run smoke     # the packaged app, end to end, in en and en-XA
python desktop/rehearse_backend.py build/backend/spend-tracker-backend/spend-tracker-backend <ledger dir>
```

The smoke test drives the packaged app with Playwright: setup, a household, a
transaction, *Back up now*, quit, relaunch (signed out, the ledger, the
language and the backup all still there), and a restore. `rehearse_backend.py`
opens a ledger made by an earlier release with this frozen backend and fails
if any table lost rows. CI runs all three on both OSes (`desktop-app` in
`.github/workflows/tests.yml`).

### Running a sandboxed Mac App Store build on this Mac

A sandboxed app will not start without a development provisioning profile
that names this Mac (its *Provisioning UDID*, from System Information →
Hardware, not the Hardware UUID). With one from the developer account:

```bash
build/freeze-venv/bin/python -m scripts.desktop_build mas
desktop/scripts/sign-mas.sh "desktop/dist/mas-dev-arm64/Spend Tracker.app" ~/path/to/profile.provisionprofile
open "desktop/dist/mas-dev-arm64/Spend Tracker.app"
```

Profiles stay out of the repository (`.gitignore`). Playwright cannot drive
the sandboxed build — it needs a debugging port, and the sandbox allows no
listener — so that one is checked by hand.

### Installing a test MSIX on Windows

Windows will not install an unsigned MSIX that starts an executable, so a test
one is signed with a throwaway certificate:

```powershell
powershell -File desktop\scripts\test-certificate.ps1 -Out C:\st-test
$env:CSC_LINK = "C:\st-test\test-signing.pfx"
$env:CSC_KEY_PASSWORD = Get-Content C:\st-test\test-signing.pass
python -m scripts.desktop_build win
```

Then the machine's owner trusts `test-signing.cer` (the script says how, and
how to remove it again) and installs `desktop\dist\*.appx`.

## Words

Every word the shell shows is in `src/words.ts`, as Lingui messages in the
client's own catalogs (`client/lingui.config.ts` extracts `desktop/src` too).
The window tells the shell which language it activated (`client/src/lib/desktop.ts`,
`src/preload.ts`), and the menus are rebuilt in it.

## Not yet

- Store signing, TestFlight, Partner Center, listings: #63, after #321 and #58.
- Electron fuses (no `--inspect`, no `ELECTRON_RUN_AS_NODE`) go on for the
  store builds in #63; the smoke test needs them off.
