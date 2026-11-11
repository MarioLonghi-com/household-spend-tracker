# The desktop app

Spend Tracker for one household on one computer: the same backend and the same
client as the server, in an Electron window, for the Mac App Store (Apple
Silicon) and the Microsoft Store (Windows 11 x64). Epic #49; this folder is
#61 and #62.

**On the stores it is called Household Spend Tracker** (#346), the name both
reserved, and the window, menus and the client's headings say so; the client
reads it from `GET /api/capabilities`. The folders below keep the name
`Spend Tracker`, so a ledger made before the rename is still the one opened.

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
- **Open with Touch ID** (#432, `src/unlock.ts`): on a Mac with a sensor,
  Profile offers it, with the password. The backend then issues a secret for
  this one purpose and keeps its hash (`app/auth/unlock.py`); the shell takes
  the secret off that answer before the page sees it, keeps it encrypted with
  `safeStorage` in `unlock.secret`, and at the next launch prompts for Touch
  ID and signs in with it before the window opens. A cancelled prompt leaves
  the password screen. Turning it off, a password change, the recovery code,
  a restore and an account reset all end it. The password is never stored.

## Where things are

| | macOS (Mac App Store) | Windows 11 (MSIX) |
|---|---|---|
| The app | `/Applications`, read-only | `C:\Program Files\WindowsApps\…`, read-only |
| The ledger, its logs, upgrade backups | the sandbox container's `Application Support/Spend Tracker/Ledger` | `%LOCALAPPDATA%\Spend Tracker\Ledger` |
| The shell's settings and log | `…/Spend Tracker/shell.json`, `shell.log` | `%LOCALAPPDATA%\Spend Tracker\shell.json`, `shell.log` |
| The Touch ID secret, encrypted (#432) | `…/Spend Tracker/unlock.secret`, only while it is on | — (Windows Hello stays open on #432) |
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
build/freeze-venv/bin/python -m scripts.desktop_build mac
```

`mac` is the plain Mac build; `mas` the Mac App Store one, to run here with a
development profile; `store` the same app for upload; `win` the MSIX and a folder.

`scripts/desktop_build.py` builds the client, stamps the commit, freezes the
backend (`desktop/backend.spec`), smoke-tests it (`smoke_backend.py`), builds
the shell, writes the third-party notices, packages it into `desktop/dist`
and reads Electron's fuses back from the result. `--from N` starts at step N.

### Fuses and notices (#63)

**The store builds have Electron's fuses on.** `scripts/fuses.cjs`, run as
electron-builder's `afterPack` hook, turns `RunAsNode`,
`EnableNodeOptionsEnvironmentVariable` and `EnableNodeCliInspectArguments`
off and `OnlyLoadAppFromAsar` and `EnableEmbeddedAsarIntegrityValidation`
on -- before anything is signed or packed, since the flip rewrites the
binary. The `store` and `win` targets get them; `mac` and `mas` stay as
Electron ships them, because the smoke test's Playwright needs the debugging
port they close. `SPENDTRACKER_FUSES=on|off` overrides either way, and the
build's last step reads the fuses back and fails if they are not as the mode
says. For a fused build Playwright cannot drive, `launch_check.py` starts it
on a fresh ledger and checks the backend came up:

```bash
SPENDTRACKER_FUSES=on build/freeze-venv/bin/python -m scripts.desktop_build mac --from 7
python desktop/launch_check.py "desktop/dist/mac-arm64/Household Spend Tracker.app"
```

**About → Licences** shows the AGPL line, the link to this version's source
(`…/tree/v<version>`, from `package.json`) and `THIRD-PARTY-NOTICES.txt`,
which `scripts/third_party_notices.py` writes at build time from what ships:
the runtime lock less what `backend.spec` excludes (`pip-licenses`), and the
packages the client's and the shell's Vite builds bundled
(`client/bundled-packages.ts`, then `license-checker-rseidelsohn`). Chromium's
own `LICENSES.chromium.html` electron-builder keeps from the Electron
download. The build refuses to package without the notices, and checks both
files are in the package.

### Checks

The shell's units, the packaged app end to end in `en` and `en-XA`, and the
upgrade rehearsal:

```bash
cd desktop && npm test
cd desktop && npm run smoke
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
desktop/scripts/sign-mas.sh "desktop/dist/mas-dev-arm64/Household Spend Tracker.app" ~/path/to/profile.provisionprofile
open "desktop/dist/mas-dev-arm64/Household Spend Tracker.app"
```

Profiles stay out of the repository: keep them with the account's other
files and pass the path, rather than copying one into `desktop/` (where
`.gitignore` would at least keep it out of a commit). Playwright cannot drive
the sandboxed build — it needs a debugging port, and the sandbox allows no
listener — so that one is checked by hand.

### Installing a test MSIX on Windows

Windows will not install an unsigned MSIX that starts an executable, so a test
one is signed with a throwaway certificate:

```powershell
powershell -File desktop\scripts\test-certificate.ps1 -Out C:\st-test
python -m scripts.desktop_build win
powershell -File desktop\scripts\sign-msix.ps1 -Package "desktop\dist\Household Spend Tracker <version>.appx" -Pfx C:\st-test\test-signing.pfx
```

`sign-msix.ps1` uses the Windows SDK's `signtool`: the copy electron-builder
carries fails on current Windows 11 with "A required function is not present".

Then the machine's owner trusts `test-signing.cer` (the script says how, and
how to remove it again) and installs `desktop\dist\*.appx`.

### Uploading to the Mac App Store

**A release tag does this by itself** (#351): `release.yml` builds the
`store` target, and once the release is public signs it, packages it and
uploads it with the secrets in the `app-store` environment, then waits for
Apple to accept the build. Submitting it for review is done in App Store
Connect. The steps below are the same ones, for doing it by hand.

The store build is signed with the **App Store** provisioning profile and the
**Apple Distribution** identity, then packaged for `/Applications` and signed
with the installer identity (*3rd Party Mac Developer Installer*, called Mac
Installer Distribution on the developer site). The profile and the App Store
Connect API key stay outside the repository; pass their paths.

```bash
build/freeze-venv/bin/python -m scripts.desktop_build store
desktop/scripts/sign-mas.sh "desktop/dist/mas-arm64/Household Spend Tracker.app" ~/path/to/appstore.provisionprofile "Apple Distribution"
desktop/scripts/package-mas.sh "desktop/dist/mas-arm64/Household Spend Tracker.app" desktop/dist/household-spend-tracker.pkg
ASC_KEY_ID=KEY_ID ASC_ISSUER_ID=ISSUER_ID desktop/scripts/upload-mas.sh desktop/dist/household-spend-tracker.pkg
```

`upload-mas.sh` validates, uploads, and then waits for Apple's processing
verdict: altool says "UPLOAD SUCCEEDED" for a build Apple goes on to refuse.
`--status DELIVERY_ID` asks about an upload already made.

The build's version is the app's (`desktop/package.json`) and must be the
version App Store Connect is preparing; a second upload of the same version
needs a new build number, `SPENDTRACKER_STORE_BUILD=2` for the `store` build.
`altool` finds the key as `~/private_keys/AuthKey_KEY_ID.p8`. The app's icon
on App Store Connect comes from this build.

### Uploading to the Microsoft Store

**A release tag builds it by itself** (#63): `release.yml` freezes the
backend on a Windows runner, packages the MSIX with the Store's identity
from three repository variables (`MSIX_IDENTITY_NAME`, `MSIX_PUBLISHER`,
`MSIX_PUBLISHER_DISPLAY_NAME`; the job fails on a tag that lacks them),
checks the identity, the version and the languages in the package's own
manifest, and once the release is public attests it and attaches
`household-spend-tracker-<version>-microsoft-store.msix` to the release.
The Store signs it, so it is unsigned and installs nowhere as it is.
Uploading it is a person's step, in Partner Center: *Packages*, then
submit. Partner Center takes one package per version, so a package fixed
after certification needs a new release.

By hand, the same package comes from the environment the script reads:

```powershell
$env:SPENDTRACKER_APPX_IDENTITY_NAME = "<Package/Identity/Name>"
$env:SPENDTRACKER_APPX_PUBLISHER = "CN=<guid>"
$env:SPENDTRACKER_APPX_PUBLISHER_DISPLAY_NAME = "<publisher display name>"
python -m scripts.desktop_build win
```

All three or none: with none set the package carries the test identity in
`electron-builder.yml`, for `sign-msix.ps1` and a test install.

## Words

Every word the shell shows is in `src/words.ts`, as Lingui messages in the
client's own catalogs (`client/lingui.config.ts` extracts `desktop/src` too).
The window tells the shell which language it activated (`client/src/lib/desktop.ts`,
`src/preload.ts`), and the menus are rebuilt in it.

## Not yet

- The store listings' translations and both submissions: #63, after #321.
  Both store builds are made by a release tag as above.
