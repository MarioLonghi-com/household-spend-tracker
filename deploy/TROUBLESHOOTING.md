# Troubleshooting the ledger

Where the database is, which one you are looking at, how to start again on
purpose, and how to get back into an account. For every way of running it:
the three container routes in [DOCKER.md](DOCKER.md), a release tarball and
a clone.

The procedures themselves live elsewhere and are linked rather than copied:
backing up and restoring in [DOCKER.md](DOCKER.md#backing-up) and
[UPGRADING.md](UPGRADING.md), upgrades in [UPGRADING.md](UPGRADING.md). This
page is for the moment something is not what you expected.

> [!important]
> **Take a backup before you change anything on this page.** Every fix below
> that deletes or moves a ledger starts with one, and the step is not
> optional. A backup does not need an outage.

---

## Contents

- [What a deployment owns](#what-a-deployment-owns)
- [Where is the ledger?](#where-is-the-ledger)
- [Which ledger is this?](#which-ledger-is-this)
- [A fresh clone opened my old ledger](#a-fresh-clone-opened-my-old-ledger)
- [Starting again on purpose](#starting-again-on-purpose)
- [It opened on the setup screen and my data is gone](#it-opened-on-the-setup-screen-and-my-data-is-gone)
- [`secret.key`: where it is, and keeping a copy](#secretkey-where-it-is-and-keeping-a-copy)
- [Getting back into an account (TOTP)](#getting-back-into-an-account-totp)
- [The node came up as `spend-tracker-1`](#the-node-came-up-as-spend-tracker-1)
- [`database is locked`, or two apps on one ledger](#database-is-locked-or-two-apps-on-one-ledger)
- [A 502 for a second, then it carries on](#a-502-for-a-second-then-it-carries-on)
- [It will not start](#it-will-not-start)
- [Backups, by route](#backups-by-route)
- [Asking for help](#asking-for-help)

---

## What a deployment owns

Two things, whatever the route:

| File | What it is | If it is lost |
|---|---|---|
| `spendtracker.sqlite3` **with its `-wal` and `-shm`** | the ledger | everything |
| `secret.key` | encrypts every authenticator (TOTP) secret; recovery codes are hashes, not encrypted with it | every member re-enrols their authenticator, through a recovery code or `scripts.reset_authenticator` |

They always sit side by side, in the **data directory**. Everything else
(the checkout, the image, the containers) is code, and code can be thrown away
and fetched again. **That is also why throwing the code away does not throw
the ledger away**: the next section is the reason a fresh clone found your
old data.

---

## Where is the ledger?

| Route | Data directory | Survives |
|---|---|---|
| **Container**, DOCKER.md sections 1, 2 and 3 | the Docker volume **`spend-tracker_ledger`**, seen inside the container as `/var/lib/spend-tracker` | `docker compose down`, `stop`, `build`, deleting the checkout, cloning again |
| **Release tarball** or **clone** | the first of: `$SPENDTRACKER_DATA_DIR` if set; `./data` if it exists in the directory you start from; otherwise `~/.local/share/spend-tracker` | `git clean -xdf`, deleting the checkout, cloning again (unless it is `./data`) |

### In a container

```bash
docker volume ls --filter name=ledger
docker volume inspect spend-tracker_ledger --format '{{ .Mountpoint }}'
```

The first lists every ledger volume on the machine; there should be one,
`spend-tracker_ledger`. Another such as `household-spend-tracker_ledger` comes
from an older checkout whose compose file did not pin the project name; see
[It opened on the setup screen](#it-opened-on-the-setup-screen-and-my-data-is-gone).

The second prints where the volume sits on the host's disk, normally
`/var/lib/docker/volumes/spend-tracker_ledger/_data`. Only root can read it.
**Look, do not copy**: in WAL mode a copied `spendtracker.sqlite3` can be
missing every recent write. Take a backup instead.

To list what is inside, through the app (the image has no shell, so no `ls`):

```bash
docker compose exec app python -c "import os; print(sorted(os.listdir('/var/lib/spend-tracker')))"
```

Run it from the directory whose `compose.yaml` you deployed with: the top of
the checkout for sections 1 and 2, `deploy/tailnet` for section 3.

### From a tarball or a clone

From the top of the checkout, because `./data` is looked for relative to the
directory you are in:

```bash
make upgrade-check
```

The first line under *What is deployed* is the data directory. `make doctor`
prints it too, with everything else on this page.

---

## Which ledger is this?

**The short way:** `make doctor` from a checkout, or in a container
`docker compose run --rm -T --entrypoint python app -m scripts.doctor`. It
names the ledger, checks the schema, whether `secret.key` opens every
authenticator, recovery codes left and the newest backup. For the sidecar,
`deploy/tailnet/check.sh` runs it after checking the containers and the
tailnet. The longer way, by hand:

The data directory says *where*; this says *what is in it*: the schema
revision, the households by name, when the first person signed up and the
row counts. It opens the database read-only and writes nothing, so it is safe
against a running instance.

**In a container** (from your compose directory):

```bash
docker compose exec -T app python - <<'EOF'
import sqlite3
from app.config import resolve_data_dir
from app.services.backup_bundle import counts, revision

d = resolve_data_dir()
db = d / "spendtracker.sqlite3"
print("data dir  :", d)
print("files     :", ", ".join(sorted(p.name for p in d.iterdir())))
print("revision  :", revision(db))
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
    print("households:", ", ".join(r[0] for r in c.execute("SELECT name FROM households")))
    print("since     :", c.execute("SELECT min(created_at) FROM users").fetchone()[0])
for table, n in counts(db).items():
    print(f"  {table:<22}{n}")
EOF
```

**From a tarball or a clone:** the same block, with the first line replaced
by `./.venv/bin/python - <<'EOF'`, run from the top of the checkout.

And the shortest answer of all, from anything that can reach the app:

```bash
curl -s localhost:8848/api/health
```

`"setup_required": true` means the ledger is empty (no owner yet).
`false` means somebody has already set it up. Behind the sidecar (section 3)
there is no port 8848 on the host; use
`docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8848/api/health').read().decode())"`
from `deploy/tailnet`.

---

## A fresh clone opened my old ledger

**What happened.** You stopped the containers, deleted or moved the
checkout, cloned it again and ran `docker compose up -d`. The app came up
with the old households, the old members and no setup screen.

**Why.** This is the design working, not a fault:

1. The ledger is in the Docker volume `spend-tracker_ledger`, not in the
   checkout. Cloning again replaces the code and leaves the volume alone.
2. Both compose files pin `name: spend-tracker`, so *any* checkout, in *any*
   directory, opens that same volume. Without the pin a clone in a new
   directory would open an empty volume and show the setup screen, which looks
   exactly like data loss.
3. `docker compose down` and `docker compose stop` remove containers, not
   volumes. Only `down -v` or `docker volume rm` deletes a volume.

The same is true of a tarball or a clone outside Docker: the default data
directory `~/.local/share/spend-tracker` is in your home directory, not in the
checkout, and every new checkout on the machine opens it.

**What to do** depends on what you wanted:

| You wanted | Do |
|---|---|
| to update the code and keep the data | nothing. That is what happened. |
| a clean, empty ledger, and the old one kept somewhere | [Starting again on purpose](#starting-again-on-purpose), steps 1 to 6 |
| a second, separate instance next to the first | a separate project name; see the end of that section |

---

## Starting again on purpose

A reset is the only operation on this page that destroys a ledger, so it
starts with a backup that has been read back.

### In a container

From your compose directory (`deploy/tailnet` for section 3):

1. **Back it up, while it runs, onto the host:**

   ```bash
   docker compose run --rm -T -v "$PWD/backups:/backups" \
     --entrypoint python app -m scripts.backup --into /backups
   ```

   It prints the folder it wrote, `backups/<stamp>/`, holding the database,
   `secret.key` and `manifest.json`.

2. **Prove the backup opens**, and look at what it holds:

   ```bash
   ls -l backups/
   docker compose run --rm -T -v "$PWD/backups:/backups:ro" \
     --entrypoint python app -m scripts.backup --verify /backups/<stamp>
   ```

   It runs an integrity check and prints `... is readable, at revision ...`
   followed by the row counts. If it prints anything else, stop here.

3. **Move the backup somewhere that is not this server**, if the ledger
   matters at all. A backup on the same disk does not survive the disk.

4. **Stop and remove the containers.** Plain `down`: no `-v`, which would
   take the Tailscale identity in `spend-tracker_ts-state` too.

   ```bash
   docker compose down
   ```

5. **Delete the ledger volume, and only that one:**

   ```bash
   docker volume rm spend-tracker_ledger
   docker volume ls --filter name=spend-tracker
   ```

   The listing should show `spend-tracker_ts-state` (section 3) and no
   `spend-tracker_ledger`. Keeping `ts-state` keeps the node's name and
   address, so nothing changes on the tailnet.

6. **Start it, creating a new schema, and check it is empty:**

   ```bash
   SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d
   docker compose logs app | grep -A2 "one-time token"
   ```

   The health answer now says `"setup_required": true`, and the browser shows
   the setup screen.

To bring the old ledger back later, restore the backup from step 1: see
[DOCKER.md, Restoring a backup](DOCKER.md#restoring-a-backup-that-is-outside-docker).

**A second instance beside the first**, rather than a reset, needs its own
project name, which gives it its own volume:

```bash
docker compose -p spend-tracker-trial up -d
```

`-p` overrides the `name:` in the file. Every later command for that instance
needs the same `-p`, or it acts on the first one. With the root compose file
the second instance also needs a different host port, so edit `ports:` in a
copy of the file; with the sidecar it also needs a different `hostname:`, or
it joins the tailnet as `spend-tracker-1`. For a short trial, the demo
database in DOCKER.md is usually simpler.

### From a tarball or a clone

1. Stop the service (`Ctrl+C` on `make serve`, or your systemd unit).
2. Back up and check it:

   ```bash
   make backup
   ls backups/
   ./.venv/bin/python -m scripts.backup --verify backups/<stamp>
   ```

3. Find the data directory (see [Where is the ledger?](#where-is-the-ledger))
   and **move it aside rather than deleting it**:

   ```bash
   mv ~/.local/share/spend-tracker ~/.local/share/spend-tracker.before-reset-$(date +%Y%m%d)
   ```

4. Create the schema and start:

   ```bash
   make migrate && make serve
   ```

---

## It opened on the setup screen and my data is gone

The opposite surprise, and almost always the ledger is still on the disk,
just not the one being opened.

**In a container**, list every ledger volume:

```bash
docker volume ls --filter name=ledger
```

- **Two volumes**, such as `spend-tracker_ledger` and
  `household-spend-tracker_ledger`: the older one is from a checkout whose
  compose file did not yet pin `name: spend-tracker`, so Compose named it
  after the directory. Copy it into the current one with a backup and a
  restore. From your compose directory, with the app stopped:

  ```bash
  docker compose stop app
  docker compose -p household-spend-tracker run --rm -T -v "$PWD/backups:/backups" \
    --entrypoint python app -m scripts.backup --into /backups
  docker compose run --rm -T -v "$PWD/backups:/backups:ro" \
    --entrypoint python app -m scripts.restore /backups/<stamp>
  docker compose up -d
  ```

  The `-p` on the second line makes the one-off container mount the *old*
  volume; the restore then runs against the current one. The restore moves
  whatever was in the current volume aside rather than deleting it.

- **One volume, empty:** `docker compose down -v` or `docker volume rm` was
  run at some point. The ledger is only in a backup now; see
  [DOCKER.md, Restoring a backup](DOCKER.md#restoring-a-backup-that-is-outside-docker).

**From a tarball or a clone:** the data directory resolved somewhere new.
Usually a `./data` that exists in one checkout and not in another, or
`SPENDTRACKER_DATA_DIR` set in one shell and not the next. Look in each:

```bash
ls -la ./data ~/.local/share/spend-tracker 2>/dev/null
echo "SPENDTRACKER_DATA_DIR=${SPENDTRACKER_DATA_DIR:-<unset>}"
```

Point the app at the right one with `SPENDTRACKER_DATA_DIR`, or move it.

---

## `secret.key`: where it is, and keeping a copy

It is generated on first start, beside the database, and every backup made
by `scripts.backup` carries a copy. To take one on its own:

**In a container:**

```bash
docker compose cp app:/var/lib/spend-tracker/secret.key ./secret.key
chmod 600 secret.key
```

**From a tarball or a clone:** it is `secret.key` in the data directory.

Keep it **apart** from the database backups you send off-site, and somewhere
only you can open: together with a member's password, the database and the
key are enough to sign in as that member. A password manager's secure note
is a reasonable home for it.

---

## Getting back into an account (TOTP)

Sign-in is a password and then a six-digit code from an authenticator app.
The authenticator's secret is stored encrypted with `secret.key`; recovery
codes are hashed. There is **no screen that shows a secret again** once it
has been enrolled, by design. A forgotten password is part 3. In order of
preference:

### 1. Use a recovery code, then set up a new authenticator

Ten recovery codes were shown once, when the account was made.

1. At the code step, choose **"I've lost my authenticator — use a recovery
   code"** and enter one. Each works once. Using one signs that member out
   everywhere, forgets every trusted browser and revokes every agent key
   they had issued; a program that should keep working needs a new key from
   the profile.
2. Signed in, open your settings by clicking **your name**, then
   **Authenticator → Set up a new authenticator**. Scan the new QR code and
   confirm with a code from it (or, again, an unused recovery code).

The old authenticator stops working the moment the new one is confirmed.
When the server's key is the problem rather than the phone, the sign-in
takes you to step 2 itself and the one code covers both; see part 4.

**Running low on codes, or worried a printed sheet was seen?** Settings →
**Recovery codes → Make new codes** replaces all ten. It takes the password and
a code from the current authenticator. A recovery code is not accepted for it,
so a leaked sheet cannot be used to mint a new one.

### 2. The member still has their phone, and wants it on a second device

Same screen: **Set up a new authenticator**, confirmed with a code from the
current one. Most authenticator apps can also export or sync an entry, which
needs nothing from this app.

### 3. An owner resets it from *Admin*

For a member with neither their authenticator nor a recovery code, and for a
forgotten password. Nothing is printed and no secret is read back. An owner
opens **Admin → People**, chooses **Reset sign-in…** on that member's row, and
ticks **Password**, **Authenticator**, or both.

- **At once, not when the link is followed**, the member is signed out
  everywhere -- every session, trusted browser and agent key ends -- and what
  was ticked stops working. A cleared authenticator takes its recovery codes
  with it.
- The dialog shows a **one-time link, once**, good for 72 hours. Hand it over
  yourself; it is not sent anywhere. Following it, the member sets a new
  password, enrols a fresh authenticator, or both -- and with a fresh
  authenticator is shown ten new recovery codes.
- Until it is followed it is listed under **Pending reset links**, a lapsed
  one marked expired: the account stays shut, and issuing another replaces
  it.
- Every owner sees the reset under **Recent sign-in changes**, and the member
  is told on the link page who reset them.

Any owner can reset any other account, another owner's included. Not their
**own**: another owner resets it, or, still signed in, they change it from
their own settings. A **disabled** account is re-enabled first.

#### When no owner can: break glass, the operator reads the secret back

For the person who runs the server, when no owner who can sign in is left
to do part 3 -- the member is the only owner, say. It decrypts the member's
stored secret with `secret.key` and prints it as an `otpauth://` link, which
any authenticator app accepts (paste it, or turn it into a QR code with
`qrencode -t ansiutf8`).

> [!warning]
> **This prints a live sign-in secret to your terminal.** Anybody who sees it
> and knows the password can sign in as that member. Run it only for your own
> account, or with the member beside you. Do not run it in a shared screen
> session or a terminal that logs, and clear the screen afterwards. It changes
> nothing in the database, and nothing records that it ran.

**In a container**, from your compose directory, with the member's sign-in
email in place of `you@example.com`:

```bash
docker compose exec -T app python - you@example.com <<'EOF'
import sys
from sqlalchemy import select
from app.auth import crypto, totp
from app.auth.email_canonical import canonical
from app.db import session_scope
from app.models import User

with session_scope() as s:
    u = s.scalars(select(User).where(User.email_canonical == canonical(sys.argv[1]))).one()
    if u.totp_secret is None:
        sys.exit("there is no authenticator to read: a reset link cleared it")
    print(totp.provisioning_uri(crypto.open_totp_secret(u.totp_secret, user_id=u.id), email=u.email))
EOF
```

**From a tarball or a clone:** the same block, starting
`./.venv/bin/python - you@example.com <<'EOF'`, from the top of the checkout.

`NoResultFound` means no member signs in with that email. *"that
authenticator secret cannot be read with this server key"* means the
`secret.key` in the data directory is not the one this ledger was made with;
see the next part. *"there is no authenticator to read: a reset link cleared
it"* means this member's account was reset: the secret is gone, not
hidden, and the reset link they were sent is the way back in -- it enrols a
new authenticator and shows new recovery codes. If the link is lost or has
lapsed, the way in is a new link, not this block.

Afterwards, have the member sign in and set up a fresh authenticator
(part 1, step 2), so the secret that was on your screen is retired.

### 4. `secret.key` is lost or wrong

Either way the app runs in **recovery mode**: the startup log says so, and
`make doctor` names the members whose authenticators the key does not open.

**Look for the original key before anybody signs in.** The key going signs
nobody out. Signing in again is what costs: each member it locks out is
asked for a recovery code, at a trusted browser too, and that code signs them
out everywhere, forgets their trusted browsers and revokes their agent keys
(part 1). Putting the original key back afterwards brings none of that back.
Back before anybody signs in, it costs nothing.

- **Wrong key**, typically after restoring a database without the key that
  came with it: every password still works and no authenticator code does.
  Restore again with the right key; the restore checks the key opens a real
  secret before it changes anything ([DOCKER.md](DOCKER.md#restoring-a-backup-that-is-outside-docker)).
  Any backup folder made by `scripts.backup` holds the right one.
- **Key truly lost:** look for it in every backup folder first, including
  off-site ones; restoring with the right key fixes everything at once. If it
  is gone for good, the app will already have made a new `secret.key` at its
  next start. The rows themselves are intact, and so are the recovery codes:
  they are hashes, not encrypted with the key.
- **Key from `SPENDTRACKER_SECRET_KEY`:** when that variable is set, the app
  never reads `secret.key`, so copying the right key into the data directory
  changes nothing. The startup log and the banner name the variable instead
  of the file in that case; it is the variable that has to hold the original
  key.

**Recovery mode** is worked out from the key every time it matters, never
stored: a member is in it while they have an authenticator enrolled and this
key cannot open it.

- Owners see a banner above every screen saying how many members that is.
  A disabled member is not counted there, in the startup log or by `make
  doctor`: they cannot sign in, so they cannot be asked. Enabled again, they
  are counted, and asked for a recovery code like anybody else.
- At sign-in such a member gives their password as usual and, as soon as it
  is right, is told the server's key was replaced and asked for a **recovery
  code** -- not for a code from the authenticator, which cannot work. A
  trusted browser does not skip this, and the password alone never signs
  anybody in. The recovery code costs what it always does: every session,
  trusted browser and agent key of theirs.
- Straight after the recovery code they set up a new authenticator. That one
  recovery code pays for both.
- **"Not now"** puts it off without losing that. Later, under their name →
  **Authenticator → Set up a new authenticator**, the same browser tab, still
  signed in and within a day, needs no other code. In another browser, or
  after that, it takes one more recovery code, and the screen asks for one in
  place of a code from the old authenticator. Until they do it, every screen
  says so above the page, with a button to the profile -- also after a reload
  on the sign-in screen, which would otherwise leave them signed in and
  nothing said.
- Until they re-enrol, anything else that asks for an authenticator code --
  issuing an agent key, a backup with its key, making somebody an owner --
  says the key was replaced rather than checking the code. A recovery code
  does not stand in there; setting up the new authenticator first does.
- The old sealed secrets are **kept, untouched**, until each member
  re-enrols. So if the original key turns up, putting it back and restarting
  ends recovery mode for everybody who has not re-enrolled: they sign in with
  the authenticator they always had. Anybody who did re-enrol in the meantime
  is then the one the key cannot open, and does it once more. And what a
  recovery code revoked stays revoked: a member who used one in the meantime
  ticks *Don't ask on this browser* again at their next sign-in, and issues
  new agent keys from the profile.
- A member with **no recovery code left** needs the operator:

  ```bash
  docker compose exec app python -m scripts.reset_authenticator you@example.com
  ```

  or `./.venv/bin/python -m scripts.reset_authenticator you@example.com`
  from a checkout. It prints a new authenticator, waits for a working code
  from it, then stores it with the current key and prints ten new recovery
  codes, once. `exec`, not `run -T`: it asks a question.

Run `make doctor` afterwards: `secret.key` should open every authenticator,
and the banner is gone. Booting into recovery mode rather than refusing to
start beside a new key was decided in #283; the mode itself is #287.

### 5. No owner can sign in: a reset link or a new owner, from the server

What an owner would do from *Admin*, for when none can:

```bash
docker compose exec app python -m scripts.reset_account you@example.com --password --authenticator
docker compose exec app python -m scripts.reset_account you@example.com --make-owner
docker compose exec app python -m scripts.reset_account you@example.com --enable
docker compose exec app python -m scripts.reset_account new@example.com --make-owner --name "Their Name" --household "Our household"
```

or `./.venv/bin/python -m scripts.reset_account ...` from a checkout. A reset
shuts the account at once and prints a one-time link, good for 72 hours, that
sets a new password, a new authenticator, or both. `--make-owner` promotes an
account; for an address with no account, `--name` creates an owner with
neither and the link sets both. `--household` (repeatable) puts the account
in a household; nothing else here does, and it is refused on its own. It says
what it will do and asks first; the link needs `--base-url` or
`SPENDTRACKER_PUBLIC_URL`. Each run is one audited act, marked in the audit log
as done from the server. It belongs to no household, so no *History* lists it.

**A disabled owner** stays disabled through all of this, and a link for a
disabled account does not open. `--enable` enables it: on its own if their
password and authenticator still work, beside `--password`/`--authenticator`
if they do not, and the link then opens. An earlier link that lapsed or was
withdrawn leaves them not working, and the command says which of the two is
gone and which switch sets it.

---

## The node came up as `spend-tracker-1`

Section 3 only. Tailscale names a node after its `hostname:` unless a machine
of that name already exists on the tailnet, in which case it adds a suffix.
The usual cause is that the `ts-state` volume was deleted (`down -v`, a new
server, a reset) while the old machine is still listed in the admin console.

Check:

```bash
docker compose exec tailscale tailscale status --json | grep '"DNSName"'
grep ^SPENDTRACKER_PUBLIC_URL .env
```

The symptom, if you do not check: invitation links point at
`spend-tracker.<tailnet>.ts.net`, which is the *old*, offline machine.

To fix: in the admin console's **Machines** page, remove the old
`spend-tracker` machine, then rename the new one to `spend-tracker` (its
**⋯ → Edit machine name**). Restart the sidecar so it picks up the
certificate for its new name:

```bash
docker compose restart tailscale
docker compose exec tailscale tailscale serve status
```

The auth key in `.env` is read only when `ts-state` is empty. If it has
expired since (90 days at most), a new registration needs a new key.

---

## `database is locked`, or two apps on one ledger

SQLite allows one writer at a time. Two causes in practice:

- **A one-off command against a running app**: seeding, restoring. Stop the
  app first; DOCKER.md says so at each.
- **Two app containers on the same volume.** Both compose files open
  `spend-tracker_ledger`, so running `docker compose up -d` from the top of
  the checkout while the sidecar deployment runs from `deploy/tailnet` starts
  a *second* app on the same ledger. Check:

  ```bash
  docker ps --filter volume=spend-tracker_ledger --format '{{.Names}}\t{{.Status}}'
  ```

  One line is right. Two: stop the one you did not mean, from its own
  directory (`docker compose stop app` at the top of the checkout, for the
  stray one in this example), and remove it with `docker compose rm app`.

---

## A 502 for a second, then it carries on

**What you see.** A request fails with a 502 (from `tailscale serve`, or
from your reverse proxy), the page says something went wrong, and a moment
later everything works again. Nothing in the app's own log explains it,
because the app never got to log anything.

**The usual cause is memory.** The kernel killed the app for going over the
container's memory limit, Docker started it again (`restart:
unless-stopped`), and the proxy answered 502 for the two or three seconds
nothing was listening. Check:

```bash
journalctl -k | grep -i "memory cgroup out of memory"
docker inspect spend-tracker-app-1 --format '{{.RestartCount}} restarts, OOMKilled={{.State.OOMKilled}}'
```

A kill line naming `python` and a `RestartCount` above zero settle it.
`OOMKilled` is often `false` even then: it describes only the *last* exit,
and the container has restarted cleanly since. The sidecar's log says the
same thing from the other side: `proxy error: … connection refused` or
`connection reset by peer`.

**The fix is the limit, not the app.** The app's working set is about 140 MiB
at rest and reaches about 500 MiB when two receipts are decoding while the
register reloads: the receipt pool is two wide on purpose, and the register
is fetched whole on purpose. Freed memory isn't handed back to the system, so
whatever the process peaked at becomes its new floor. `mem_limit: 768m` is
the minimum, and the machine needs room for it on top of everything else
([DOCKER.md, Before you start](DOCKER.md#before-you-start)). Raise the limit
in a `compose.override.yaml` beside the compose file, not by editing the file
itself, so a `git pull` doesn't take it back:

```yaml
services:
  app:
    mem_limit: 768m
```

If the machine cannot give it 768 MiB, `MALLOC_ARENA_MAX: "2"` in the app's
`environment:` trims about a tenth off the high-water mark. It makes a kill
less likely; it doesn't make a small limit safe. Measurements: #14.

---

## It will not start

[DOCKER.md, When it will not start](DOCKER.md#when-it-will-not-start) has the
container cases: a root-owned volume, a schema mismatch, `Invalid host
header`, a sign-in that loops, a sidecar that never joins. Two rules from
there are worth repeating here, because both are about the ledger:

- **A schema mismatch is the app refusing to guess.** Do not set
  `SPENDTRACKER_AUTO_MIGRATE=1` to make it go away; that migrates without a
  backup. Run the image the volume expects, or upgrade deliberately
  ([UPGRADING.md](UPGRADING.md)).
- **`down -v` deletes the ledger**, not just the containers. It is only right
  before anything has been written.

---

## Backups, by route

| | Take one | Check one | Restore |
|---|---|---|---|
| **Container** | `docker compose run --rm -T -v "$PWD/backups:/backups" --entrypoint python app -m scripts.backup --into /backups` | `... -m scripts.backup --verify /backups/<stamp>` | [DOCKER.md](DOCKER.md#restoring-a-backup-that-is-outside-docker) |
| **Tarball or clone** | `make backup` | `./.venv/bin/python -m scripts.backup --verify backups/<stamp>` | `make restore FROM=backups/<stamp>` ([UPGRADING.md](UPGRADING.md)) |
| **Any, from the browser** | *Application management → Back the database up*, then download | | `make restore FROM=<the .zip>`, or the container restore with the zip |

What every route shares:

- **Never copy `spendtracker.sqlite3` by hand.** Recent writes live in the
  `-wal` file until a checkpoint.
- **The backup is only on the server until you move it.** `backups/` on the
  same disk protects you from a bad migration, not from losing the disk.
- **A backup that has never been restored is a belief.** UPGRADING.md has a
  table to record the one rehearsal that makes it a capability.
- **For off-site storage that deduplicates** (restic, borg, Time Machine),
  add `--method backup`; DOCKER.md has the measurement.

---

## Asking for help

These show the state without giving anything away. Run them from your
compose directory and paste the output:

```bash
git describe --tags
docker compose version
docker compose ps
docker volume ls --filter name=spend-tracker
docker compose logs --tail 50 app
```

**Do not paste** `.env` (it holds the Tailscale auth key), `secret.key`,
the database, a backup, or a log from before setup was finished (it holds the
one-time setup token). `sql.log`, if it exists, holds the ledger in plain
text.
