# Running it with Docker

One image, one volume holding the ledger, and a choice about where it runs
and how people reach it. Pick a row, follow that section once, and then
everything else on this page (the setup token, demo data, restoring a
backup, upgrades, logs, troubleshooting) is the same for all three.

| Where it runs | How it is reached | Compose file | Section |
|---|---|---|---|
| **Your own computer** (laptop or desktop) | `http://localhost:8848`, this machine only | `compose.yaml` | [1](#1-on-your-own-computer) |
| **A server**, Tailscale installed on the server | `https://<server>.<tailnet>.ts.net`, from any device on your tailnet | `compose.yaml` | [2](#2-on-a-server-with-tailscale-on-the-host) |
| **A server**, Tailscale as a sidecar container | `https://spend-tracker.<tailnet>.ts.net`, its own tailnet node | `deploy/tailnet/compose.yaml` | [3](#3-on-a-server-with-tailscale-as-a-sidecar) |

Which one:

- **Trying it out, or a ledger only you use at one desk:** section 1.
- **One household, one server, nothing else on it:** section 2. The server
  itself is the tailnet node and the app sits behind it.
- **A server that hosts several applications:** section 3. Each app gets its
  own tailnet address, its own certificate and its own access rule, instead
  of all of them sharing the server's.

Every command on this page has been run against a real daemon, on both base
images. Where a flag looks optional and is not, the reason is next to it.

**Something not what you expected about the data**, such as a fresh clone
opening an old ledger, a reset, or a member locked out of their
authenticator: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Before you start

- **Docker with Compose v2.** `docker compose version` should print a `v2`
  version. On a Mac or Windows machine that is Docker Desktop or OrbStack. On
  a Debian or Ubuntu server, install from Docker's own apt repository rather
  than the distribution's package, so the compose plugin comes with it:

  ```bash
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
  sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER"   # then log out and back in
  ```

  Replace `debian` with `ubuntu` in both URLs on Ubuntu. Any other Linux
  with Docker Engine and the compose plugin works the same.

- **A checkout of this repository.** On a server, check out a release tag so
  the box runs a version with a name:

  ```bash
  git clone https://github.com/MarioLonghi-com/household-spend-tracker.git
  cd household-spend-tracker
  git checkout v0.5.1
  ```

- **About 1 GB of RAM and 1 GB of disk** for the image and a household's
  ledger. The database for five years of receipts measures under 2 GB.

---

## 1. On your own computer

The app answers on this machine only, at `http://localhost:8848`. Nothing on
your network can reach it, and no Tailscale is involved.

```bash
docker compose build
SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d
curl -s localhost:8848/api/health
```

Then open <http://localhost:8848> and go to **The setup token** below.

Three things worth knowing about this mode:

- **`SPENDTRACKER_AUTO_MIGRATE=1` is for the first start only.** A brand-new
  volume has no schema, and that flag creates it. After that run plain
  `docker compose up -d`: a restart is not a decision to change a schema, and
  with the flag off the app refuses to boot against a database that is behind
  or ahead and says which revision each side is at. That is what you want from
  a container that came back at 04:00.
- **`localhost` is enough for the `Secure` cookie.** Browsers treat
  `http://localhost` as a trustworthy origin, so sign-in works over plain HTTP
  here. It would not at `http://192.168.1.50:8848`: the browser silently drops
  the cookie and the sign-in loops with nothing in any log. That is why the
  port is published to `127.0.0.1` and why this mode is for one machine.
- **For other devices in the house, use a server.** Sections 2 and 3 give you
  HTTPS and a name. For one evening on the sofa the README's `make lan`
  exists, with its warnings.

---

## 2. On a server, with Tailscale on the host

Tailscale runs **on the server**, and `tailscale serve` terminates HTTPS for
the server's own tailnet name and proxies into the container on loopback.
Every device on your tailnet reaches the app at
`https://<server>.<tailnet>.ts.net`. Nothing on the LAN or the internet can.

**Before:** Tailscale installed on the server and signed in
(`tailscale status` shows the server), and HTTPS certificates enabled for
the tailnet in the admin console (DNS → HTTPS Certificates).

```bash
docker compose build
SPENDTRACKER_PUBLIC_URL=https://<server>.<tailnet>.ts.net \
SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d
curl -s localhost:8848/api/health
sudo tailscale serve --bg 8848
```

Then open `https://<server>.<tailnet>.ts.net` from any device on the tailnet
and go to **The setup token** below. `tailscale serve --bg` persists across
reboots; `tailscale serve status` shows it.

Put `SPENDTRACKER_PUBLIC_URL` in a `.env` file next to `compose.yaml` so it
is not retyped on every `up`:

```bash
echo "SPENDTRACKER_PUBLIC_URL=https://<server>.<tailnet>.ts.net" >> .env
```

What the compose file already does for you, and why:

- **The port is published to `127.0.0.1` only.** `tailscale serve` connects
  from the server's loopback, so loopback is the only place the container
  needs to answer. Publishing `8848:8848` would put the ledger on every
  interface in plain HTTP.
- **The session cookie stays `Secure`**, because the browser only ever sees
  HTTPS. Do not turn `SPENDTRACKER_COOKIE_SECURE` off here.
- **Host names.** The app answers to loopback, IP literals and `*.ts.net` by
  default, which is exactly what `tailscale serve` and the healthcheck send.
  If a different reverse proxy reaches it under a name of its own, set
  `SPENDTRACKER_ALLOWED_HOSTS`; it replaces the default rather than adding to
  it, so include the entries you still use.
- **Forwarded addresses.** Requests arrive from Docker's bridge, so
  `FORWARDED_ALLOW_IPS` trusts that range and the per-address sign-in limits
  apply per tailnet peer rather than to everybody at once. Narrow it to your
  bridge's gateway if you know it; never set it to `*`.
- **`SPENDTRACKER_PUBLIC_URL`** is where invitation links point. Set it to the
  address people actually use.

> **Never `tailscale funnel`.** Funnel publishes the app to the open internet.
> Everything on this page assumes the only way in is your tailnet.

**Hosting several applications on this server?** They would all share the
server's one tailnet name and one certificate, with ports or paths to tell
them apart. Section 3 gives each one its own node instead.

---

## 3. On a server, with Tailscale as a sidecar

Two containers that share one network namespace: a Tailscale container that
joins your tailnet as a node called `spend-tracker`, and the app, which lives
inside the Tailscale container's network and listens only on loopback there.
The result is `https://spend-tracker.<tailnet>.ts.net` with its own `100.x`
address, its own certificate and its own tag to write access rules against.
The server does not need Tailscale installed at all, and a second app gets a
second pair of containers and a second name.

Three names recur below. Find yours before you start:

| Placeholder | What it is | Where to find it |
|---|---|---|
| `<tailnet>` | your tailnet's DNS name, such as `tail1a2b3c` in `tail1a2b3c.ts.net` | see **Finding your tailnet name** below |
| the version | the release you checked out, such as `0.5.1` | see **Finding the version** below |
| the auth key | `tskey-auth-…`, made in step 2 | the admin console shows it **once**, when you make it |

### Before, in the Tailscale admin console

These three steps are done in a browser, not on the server.

#### 1. Access controls: let the key carry a tag, and say who may reach it

**The goal, in one sentence:** the node `spend-tracker` carries the tag
`tag:spend-tracker`, and the only traffic allowed to it is HTTPS (TCP 443)
from the people in your tailnet. Everything else in this step is the
mechanics of saying that.

Two separate things are being declared, and they fail differently:

- **`tagOwners` is required.** It says who may put the tag on a machine. If
  `tag:spend-tracker` is not in it, step 2 cannot make a key with that tag,
  and the console will not offer it.
- **The `grants` rule is what restricts access**, and it only bites once the
  policy no longer allows everything. A new tailnet's policy allows every
  device to reach every other device on every port. While that rule is in the
  file, your new rule is harmless but adds nothing. The app is still safe in
  that state (it answers HTTPS on 443 and nothing else; see *How it is wired*),
  but the tag is not yet doing any work.

Open **Access controls** (<https://console.tailscale.com/admin/acls>), which
shows the policy file as JSON. **Merge** the entries below into the keys that
are already there. Do not paste a second `"tagOwners"` or `"grants"` key
beside an existing one: add the lines *inside* it.

```jsonc
{
  "tagOwners": {
    // Who may tag a machine as tag:spend-tracker. Admins only.
    "tag:spend-tracker": ["autogroup:admin"]
  },
  "grants": [
    // Every member of the tailnet may reach it, on HTTPS only.
    { "src": ["autogroup:member"], "dst": ["tag:spend-tracker"], "ip": ["tcp:443"] }
  ]
}
```

- `autogroup:admin` and `autogroup:member` are built in: the tailnet's admins,
  and every user in it. Narrow `src` to a group (`"group:household"`) once you
  have defined one in `groups`.
- `"ip": ["tcp:443"]` is the point of the rule. Port 443 and nothing else.
- An older policy may use `"acls"` with `"action": "accept"` rather than
  `"grants"`. Either works; the equivalent line is
  `{ "action": "accept", "src": ["autogroup:member"], "dst": ["tag:spend-tracker:443"] }`.
- **Only if you want the restriction to be real:** remove the allow-all rule,
  which looks like `{"src": ["*"], "dst": ["*"], "ip": ["*"]}` (or, in the
  older form, `{"action": "accept", "src": ["*"], "dst": ["*:*"]}`). **That
  changes access for every device in the tailnet, not only this one.** Add a
  rule for every other device you still need to reach first.

Press **Save**. The editor checks the file and refuses to save one that does
not parse, so a mistake here cannot half-apply.

**If any of this does not match what you see,** the console has moved on
since this page was written. Tailscale's own pages are the authority:
[tags](https://tailscale.com/kb/1068/tags),
[grants](https://tailscale.com/kb/1324/grants),
[the policy file and ACLs](https://tailscale.com/kb/1018/acls). The goal
sentence above is what has to stay true, whatever the syntax becomes.

#### 2. Make an auth key

**Settings → Keys → Generate auth key**
(<https://console.tailscale.com/admin/settings/keys>):

| Option | Set it to | Why |
|---|---|---|
| Reusable | yes | a `down -v` or a rebuilt server can register again with the same key |
| Ephemeral | **no** | an ephemeral node is deleted when it goes offline, so a reboot would lose it |
| Tags | `tag:spend-tracker` | the node is born tagged, so it needs no human login and never expires |
| Expiration | your choice, at most 90 days | the key is only needed for the *first* registration; the node outlives it |

Copy the key now; the console does not show it again. **It is a
credential**: it goes into the `.env` file below and nowhere else (not a
chat, not a ticket, not a commit). See
[auth keys](https://tailscale.com/kb/1085/auth-keys).

#### 3. Turn on MagicDNS and HTTPS certificates

**DNS** page (<https://console.tailscale.com/admin/dns>): MagicDNS on, then
**Enable HTTPS** under *HTTPS Certificates*. Machine names you give a
certificate are published in the public Certificate Transparency log, which
is one reason the node is called `spend-tracker` and not after a person. See
[enabling HTTPS](https://tailscale.com/kb/1153/enabling-https).

#### Finding your tailnet name

The same DNS page shows it at the top, as **Tailnet DNS name**:
`tail1a2b3c.ts.net`, or a two-word name such as `cat-crocodile.ts.net` if
somebody picked one. `<tailnet>` on this page is the part before `.ts.net`.

From any machine that already runs Tailscale (your laptop, say), without the
browser:

```bash
tailscale status --json | grep MagicDNSSuffix
```

It prints `"MagicDNSSuffix": "tail1a2b3c.ts.net",`. Once the sidecar is
running, the node itself says it; see step 6 below.

### On the server

Every command from here on runs on the server, from the checkout.

#### Finding the version

The release you checked out, read three ways. They should agree:

```bash
git describe --tags
grep __version__ app/__init__.py
```

`git describe` prints `v0.5.1` on a tag, or `v0.5.1-3-gabc1234` three commits
past it. The second prints `__version__ = "0.5.1"`: that is the number the
running app will report at `/api/health`, and the one `.env` wants, **without
the `v`**.

#### 1. Go to the sidecar's directory

```bash
cd deploy/tailnet
ls -a
```

`ls -a` should list `.env.example`, `compose.yaml` and `serve.json`.
`compose.yaml` here is a **different file** from the one at the top of the
checkout: it describes the two containers. Docker Compose reads the
`compose.yaml` in the directory you are standing in, which is why the
location matters (see **From now on** below).

#### 2. Make this deployment's `.env`

```bash
cp .env.example .env && chmod 600 .env
ls -l .env
```

- `cp .env.example .env` creates **this deployment's own settings file** from
  the committed template. `.env.example` is in git and holds placeholders;
  `.env` is git-ignored and will hold your real auth key and address. Compose
  reads `.env` from the directory it runs in, automatically, and substitutes
  its values into `compose.yaml`.
- `chmod 600 .env` makes it readable and writable by your user only, because
  it is about to hold a credential.
- `ls -l .env` should start `-rw-------`. Anything else, run the `chmod` again.

#### 3. Fill in `.env`

Open it in a terminal editor. `nano` is on almost every Debian and Ubuntu
server (`sudo apt-get install -y nano` if not):

```bash
nano .env
```

Change the three lines that say `REPLACE-ME` or carry a version:

```ini
TS_AUTHKEY=tskey-auth-k1a2b3c...           # the whole key from step 2, nothing around it
SPENDTRACKER_PUBLIC_URL=https://spend-tracker.tail1a2b3c.ts.net
SPENDTRACKER_VERSION=0.5.1                 # from "Finding the version", no leading v
```

No quotes, no spaces around `=`, no trailing space after the key: Compose
would keep them as part of the value, and Tailscale would refuse the key.
In `nano`: paste with your terminal's paste (often Ctrl+Shift+V or
right-click), save with **Ctrl+O** then **Enter**, quit with **Ctrl+X**.

Or set the version straight from the checkout instead of typing it:

```bash
sed -i "s/^SPENDTRACKER_VERSION=.*/SPENDTRACKER_VERSION=$(git describe --tags --abbrev=0 | sed 's/^v//')/" .env
```

**Check it**, without printing the key to the screen:

```bash
grep -v '^#' .env | grep . | sed 's/^\(TS_AUTHKEY=tskey-auth-\).*/\1<hidden>/'
docker compose config --quiet && echo "compose file and .env: OK"
```

The first shows the three settings with the key masked; nothing should still
say `REPLACE-ME`. The second has Compose read `compose.yaml` with `.env`
substituted in, and prints `OK` only if every required value is present. If
one is missing it names it, for example *"put a tagged Tailscale auth key in
deploy/tailnet/.env"*.

#### 4. Build the image

```bash
docker compose build
docker image ls household-spend-tracker
```

Builds the app's image from the checkout (two levels up). It takes a few
minutes the first time. The listing should show a tag equal to
`SPENDTRACKER_VERSION`, so `docker image ls` on this box always says which
release it is running.

#### 5. Start both containers, creating the database

```bash
SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d
docker compose ps
```

- `up -d` starts the `tailscale` container first, then the `app` inside its
  network, both in the background (`-d`).
- `SPENDTRACKER_AUTO_MIGRATE=1` lets the app create the schema in a brand-new
  volume. **First start only.** Every later start is plain `docker compose up -d`;
  see section 1 for why.
- **If a ledger already exists in the volume `spend-tracker_ledger`, this
  opens it.** The volume outlives containers, images and checkouts, so a fresh
  clone does not mean a fresh database. [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
  explains, and has the commands for a deliberate reset.

`docker compose ps` should show two services, both `running`, and `app`
turning `(healthy)` within about 30 seconds. `(health: starting)` means wait;
`restarting` or `exited` means read `docker compose logs app`.

#### 6. Check the sidecar joined the tailnet

```bash
docker compose logs tailscale | tail -20
docker compose exec tailscale tailscale status --self
docker compose exec tailscale tailscale status --json | grep -E '"(DNSName|MagicDNSSuffix)"'
docker compose exec tailscale tailscale serve status
```

- The log should end with the node connecting and the serve config being
  applied, without `invalid key` or `requested tags are invalid`.
- `status --self` shows this node's `100.x` address and name.
- The `DNSName` line is the node's real name. **It must match the host in
  `SPENDTRACKER_PUBLIC_URL`.** If it reads `spend-tracker-1.<tailnet>.ts.net`,
  a machine called `spend-tracker` already exists (usually an earlier
  install); see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-node-came-up-as-spend-tracker-1).
- `serve status` should show `https://spend-tracker.<tailnet>.ts.net` proxying
  to `http://127.0.0.1:8848`.

#### 7. Check the app, from the server itself

```bash
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8848/api/health').read().decode())"
```

This asks the app from inside its own container, on loopback, the same request
the image's healthcheck makes, so it works before any tailnet device can reach
it. `python -c` rather than `curl`, because the runtime image has no shell
and no `curl` (see **The setup token**). It should print:

```json
{"status":"ok","version":"0.5.1","commit":"…","setup_required":true}
```

`version` is the release you built. `setup_required: true` is correct for a
new ledger. **`false` on a server you meant to be new means the volume
already held a ledger**; stop here and read
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#a-fresh-clone-opened-my-old-ledger).

#### 8. Check it from another device on the tailnet

From your laptop or phone, signed in to the same tailnet:

```bash
curl -sI https://spend-tracker.<tailnet>.ts.net/api/health
curl -s --max-time 3 http://spend-tracker.<tailnet>.ts.net:8848
```

The first should answer `HTTP/2 200` over a real certificate, and the second
should be refused or time out. The second line is the point of the design:
there is no plain-HTTP way in. The first request after a start can take a few
seconds while the certificate is issued.

Then open `https://spend-tracker.<tailnet>.ts.net` and go to **The setup
token** below.

### From now on, run every command on this page from `deploy/tailnet`

Every `docker compose ...` command acts on the `compose.yaml` **in the
directory you run it from**. The sidecar deployment is described by
`deploy/tailnet/compose.yaml`, so that is where you stand for the setup
token, backups, restores, upgrades and logs below. Run the same command from
the top of the checkout and it reads the *other* `compose.yaml`, the
single-container one from sections 1 and 2.

That mistake is less dangerous than it sounds: both files pin the project
name `spend-tracker`, so both point at the **same** volume
`spend-tracker_ledger` and you cannot open a second, empty ledger by
accident. But from the top of the checkout `docker compose ps` will not show
the sidecar, and `docker compose up -d` would start a second app container
with a published port. If a command says *no such service: tailscale*, or
shows one container where you expected two, check `pwd`.

`-T`, `--entrypoint python` and `$PWD/backups` all mean what they mean
elsewhere; `backups/` is git-ignored wherever it is created.

### Is it working? A status report

Run these whenever you want to know the state of the deployment, from
`deploy/tailnet` in your checkout. None of them writes anything.

**1. Containers.** Healthy: `tailscale` running, `app` running `(healthy)`.

```bash
docker compose ps
```

**2. Which code.** Healthy: the tag, the image's tag and the `version` in the
health answer are the same release.

```bash
git describe --tags
docker image ls household-spend-tracker
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8848/api/health').read().decode())"
```

**3. The tailnet.** Healthy: the node's name equals the host in
`SPENDTRACKER_PUBLIC_URL`, and serve proxies 443 to `127.0.0.1:8848`.

```bash
docker compose exec tailscale tailscale status --self
docker compose exec tailscale tailscale serve status
grep ^SPENDTRACKER_PUBLIC_URL .env
```

**4. Where the ledger is, and which one it is.**

```bash
docker volume ls --filter name=spend-tracker
docker volume inspect spend-tracker_ledger --format '{{ .Mountpoint }}'
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

Healthy: two volumes, `spend-tracker_ledger` and `spend-tracker_ts-state`.
The mountpoint is where the database sits **on the server's disk**, normally
`/var/lib/docker/volumes/spend-tracker_ledger/_data` and readable by root
only; inside the container the same directory is `/var/lib/spend-tracker`.
The last command opens the database read-only and prints its schema revision,
the households by name, when the first member signed up and the row counts:
that is how you tell *which* ledger is open.

**5. Anything wrong lately.** Healthy: nothing, or nothing new.

```bash
docker compose logs --since 1h app | grep -iE "error|warn|refus" | tail -20
```

Never copy the database out of that mountpoint by hand; take a backup (see
**Backing up**).

### How it is wired, in case you need to change it

- **`network_mode: service:tailscale`** puts the app inside the sidecar's
  network namespace. `tailscale serve` terminates TLS on 443 and proxies to
  `127.0.0.1:8848` inside that namespace. There is no `ports:` entry in the
  file, and the test suite fails if one appears.
- **`SPENDTRACKER_HOST=127.0.0.1`.** In a shared namespace the namespace *is*
  the tailnet node, so the root file's `0.0.0.0` would put plain HTTP on
  `spend-tracker.<tailnet>.ts.net:8848` beside the HTTPS on 443. The access
  rule above closes that port too; the environment variable closes it in the
  app, where a policy edit cannot reopen it.
- **`FORWARDED_ALLOW_IPS=127.0.0.1`.** The proxy connects from loopback, so
  uvicorn's default trust applies and sign-in limits are per tailnet peer.
- **The sidecar needs `/dev/net/tun` and `net_admin`.** If the host cannot
  grant them (an unprivileged LXC without the device passed through, or
  rootless Docker or Podman), set `TS_USERSPACE: "true"` and delete the
  `devices:` and `cap_add:` entries. Userspace networking is slower, which
  does not matter for one ledger.
- **The auth key is read once**, when the `ts-state` volume is empty. After
  that the node's identity lives in the volume. `docker compose down -v`
  deletes it **and the ledger with it**, and the node registers again as a
  new machine (and the old one has to be removed in the console).
- **Recreating the sidecar takes the app's network with it.** A restart
  (`docker compose restart tailscale`) keeps the container and is harmless.
  A recreate, after changing its image or configuration, needs
  `docker compose up -d`, which recreates both in order.
- **`serve.json`** is the whole proxy configuration. **Never add
  `AllowFunnel`** to it.

---

## The setup token

The first screen asks for a one-time token. It is printed to the container
log at startup and written to a file in the volume:

```bash
docker compose logs app | grep -A2 "one-time token"
docker compose exec app python -c "print(open('/var/lib/spend-tracker/setup-token').read())"
```

`python -c` rather than `cat`, because **the runtime image has no shell and no
coreutils**. That is most of the point of the Chainguard base, and it is the
first thing that surprises people.

The token changes on every restart until setup is finished, so a token that
leaked into a screenshot is stale by the next `docker compose restart`.

**Until setup is finished the container log is a credential**: whoever reads
the token first can claim the instance. Finish setup before pointing a log
collector or a persistent logging driver at this container.

> **If the log shows uvicorn starting but nothing from the app**, the image
> predates 2026-09-23 and wrote its output to a file in the volume instead.
> `git pull && docker compose build`.

---

## A demo database, to look around

Six months of invented transactions, receipts and members, with the
credentials printed at the end.

```bash
docker compose stop app
docker compose run --rm -T --entrypoint python app \
  -m scripts.seed_demo --reset --months 6 | tee demo-credentials.txt
docker compose up -d
```

- **`--reset` drops everything first.** Never against a ledger you care about.
- **`-T` is not optional.** Without it `docker compose run` allocates a TTY,
  and in a non-interactive shell (a script, a CI step, an agent) the output is
  discarded. The seed prints the demo password, the TOTP secret and ten
  recovery codes, none of them recoverable afterwards. Measured: without `-T`
  the command exits 0 and prints nothing.
- **`--entrypoint python` runs anything other than the server.** The image's
  entrypoint is the server; overriding it gives you a plain interpreter.
- **Stop the app first.** SQLite takes a write lock; seeding against a running
  instance blocks or interleaves.

---

## Restoring a backup that is outside Docker

A backup you are holding on the computer (copied from another machine, pulled
out of off-site storage, downloaded from the Application screen) is not
visible to the container until you mount the folder it is in. That is the
one idea in this section: **mount the host folder, then name the file by its
path inside the container.**

The restore script accepts three shapes:

| What you have | Where the key comes from |
|---|---|
| A **folder** made by `make backup` or `scripts.backup`: `spendtracker.sqlite3`, `secret.key`, `manifest.json` | inside the folder, automatically |
| A **zip** downloaded from the Application screen | inside the zip if "Include secret.key" was ticked; otherwise `--key` |
| A bare **`spendtracker.sqlite3`** | always `--key` |

`secret.key` is not optional: it encrypts every authenticator secret. The script checks the key actually opens the backup
before it touches anything, and refuses if it does not.

### Step by step

Say the backup and the key are at `/srv/restore/`:

```
/srv/restore/
    spendtracker.sqlite3
    secret.key
```

1. **Stop the app.** The script cannot tell from inside a one-off container
   whether the real one is running, so this is on you.

   ```bash
   docker compose stop app
   ```

2. **Run the restore with the folder mounted.** Left of the colon is the host
   path, absolute; right of it is where the container sees it. The paths in
   the command after `scripts.restore` are the container-side ones.

   ```bash
   docker compose run --rm -T \
     -v "/srv/restore:/backups:ro" \
     --entrypoint python app \
     -m scripts.restore /backups/spendtracker.sqlite3 --key /backups/secret.key
   ```

   For a folder made by the backup script, point at the folder and leave
   `--key` off:

   ```bash
   docker compose run --rm -T \
     -v "/srv/restore:/backups:ro" \
     --entrypoint python app \
     -m scripts.restore /backups/20260101-120000
   ```

   For a zip, point at the zip, and add `--key` only if it was downloaded
   without one:

   ```bash
   docker compose run --rm -T \
     -v "/srv/restore:/backups:ro" \
     --entrypoint python app \
     -m scripts.restore /backups/spendtracker-20260101T120000Z.zip --key /backups/secret.key
   ```

3. **Read the summary and type `restore`.** Before it changes anything it
   prints when the backup was taken, the app version and schema revision it
   is at, a row count per table, which key it will use and whether that key
   opens the backup, and then waits:

   ```
   Type 'restore' to go ahead:
   ```

   Anything else leaves the volume untouched. From a script, add `--yes` to
   skip the question.

4. **Start it and check.**

   ```bash
   docker compose up -d
   curl -s localhost:8848/api/health
   ```

   Sign in with an account from the backup. If the authenticator is refused,
   the wrong `secret.key` went in: restore again with the right one.

### What it does to the volume

Whatever was there is **moved aside, not deleted**: the database, its `-wal`
and `-shm` files, and the key if a new one came in, each renamed with a
`.before-restore-<stamp>` suffix inside the volume. Then the backup's database
and key are copied in with private permissions.

**The revision it prints is the one the image has to match.** A backup taken
at a newer version than the image you are running will not boot (the schema
guard says which side is ahead); build that version first. A backup from an
older version boots after an upgrade, see **Upgrading**.

### The key is lost

`--without-key` restores anyway. Every row is intact and readable; every
member's authenticator is refused. Their recovery codes still work (they are
hashes, not encrypted with the key), so each person signs in with one and sets
up a new authenticator; anyone with none left is given one with
`scripts.reset_authenticator`. Only for a key that is truly unrecoverable;
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#4-secretkey-is-lost-or-wrong) has the
steps.

### Checking a backup without restoring it

```bash
docker compose run --rm -T -v "/srv/restore:/backups:ro" \
  --entrypoint python app -m scripts.backup --verify /backups
```

Integrity check, revision and row counts, and whether `secret.key` is beside
it. Useful before a long upgrade, or for a backup somebody emailed you.

> **Stop the app yourself, every time.** Copying a SQLite file in under a live
> process is how a write-ahead log and its database start disagreeing. The
> script checks the port, but from a one-off container that check sees only
> its own network namespace and always finds it free.

---

## Backing up

```bash
docker compose run --rm -T \
  -v "$PWD/backups:/backups" \
  --entrypoint python app \
  -m scripts.backup --into /backups
```

This does not need an outage: `VACUUM INTO` reads across the write-ahead log,
so the copy is consistent while the app keeps running. It then reopens the
copy and counts its rows before reporting success.

- **Never copy the database file by hand.** In WAL mode recent writes sit in
  `spendtracker.sqlite3-wal` until a checkpoint; the main file has been 4 KB
  while the WAL held 1.7 MB.
- **`--into` with a mount is what gets the backup off the volume.** Without
  it the backup lands in the volume, which protects you from a bad migration
  and not from losing the volume.
- **For an off-site copy that deduplicates, add `--method backup`:**

  ```bash
  docker compose run --rm -T -v "$PWD/backups:/backups" \
    --entrypoint python app -m scripts.backup --into /backups --method backup
  ```

  `VACUUM INTO` repacks the database, so restic, borg or Time Machine re-send
  most of it every time (8% of blocks reused, 666 MiB of transfer for 2.5 MiB
  of new receipts). `--method backup` preserves page numbering: 98.5% reuse,
  11.2 MiB. Both measured against a real 725 MiB database.

---

## Upgrading

```bash
# 1. Back up, while it is still running.
docker compose run --rm -T -v "$PWD/backups:/backups" \
  --entrypoint python app -m scripts.backup --into /backups

# 2. Get the new code and build it.
#    On a server built from a tag:  git fetch --tags && git checkout v0.5.2
git pull
docker compose build

# 3. Ask the NEW image what it would do to your CURRENT volume.
docker compose run --rm -T --entrypoint python app -m scripts.upgrade --check

# 4. Read the `rolling back:` line for every migration it lists. Decide now.

# 5. Stop, migrate, start.
docker compose stop app
SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d

# 6. Prove what is running.
curl -s localhost:8848/api/health
```

Behind the sidecar there is no port 8848 on the host, so step 6 uses the https
address from section 3 instead.

Step 3 is the one that is easy to skip and is the point of the whole page. It
names every migration between your database and that image and says whether
rolling each one back would lose anything. If any is `lossy`, the only faithful
way back is the backup from step 1, with everything written since going with
it. "Not checked" against `main` is expected: there is no git in the image.

In section 3 the sidecar image can be updated independently of the app by
changing the pinned tag and running `docker compose up -d`; it owns no data.

### Rolling back

```bash
docker compose stop app
git checkout <the previous tag>
docker compose build
docker compose run --rm -T -v "$PWD/backups:/backups:ro" \
  --entrypoint python app -m scripts.restore /backups/<the stamp from step 1>
docker compose up -d
```

If the release was `clean`, checking out the old code and restarting is enough
and the restore is unnecessary. If it was `lossy`, the restore is the only
honest option. [UPGRADING.md](UPGRADING.md) has the three cases in full.

---

## Reading the logs

Two different things, and it is worth knowing which you are looking at.

```bash
docker compose logs -f app
```

That is the container's stdout: what uvicorn wrote to the console, the boot
lines and anything that crashed. It is what you want when the container
**will not start**, because at that point there may be no volume to write
into.

Everything else goes into **files in the volume**, which the app itself shows
under *Application management → Log files*. Three of them, separated on
purpose:

| | |
|---|---|
| `app.log` | what the instance did. The one you read, and the one you would send somebody. |
| `access.log` | one line per HTTP request. Mechanical, and it drowns the rest when they share a file. |
| `sql.log` | **only written at the "Verbose, with SQL" style, and it holds the ledger in plain text.** Its own file so the other two stay safe to share. |

Rotated at a megabyte, five deep; a rotated file is named for the moment it
was closed, `app-20260923-131545.log`. To get them out of the volume:

```bash
docker compose cp app:/var/lib/spend-tracker/logs ./logs
```

Changing the logging style writes a line into all three, on both sides of the
change and with who did it, including when you turn it *down*. A file that
goes quiet tells you why rather than looking like an instance that died.

---

## When it will not start

Questions about the ledger itself rather than the container (which one is
open, starting again, `secret.key`, authenticators) are in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

### `[Errno 13] Permission denied: '/venv'` during the build

An old checkout. Chainguard's `-dev` base runs as the nonroot user, and the
build now does `USER root` in the stage that creates the virtualenv. `git pull`
and build again.

### `/var/lib/spend-tracker is not writable by uid 65532`

**A named volume keeps the ownership it was created with**, however many times
you rebuild the image. A volume created by a build from before this was fixed
is root-owned for ever, and `docker compose up` will not mention it.

If the ledger in it does not matter yet, a first run that failed:

```bash
docker compose down -v && docker compose up -d
```

If it does:

```bash
docker compose run --rm --user root app --fix-ownership
docker compose up -d
```

That is a flag on the entrypoint rather than a `chown`, because **there is no
`chown` in the image**. No shell and no package manager is most of the point of
the Chainguard base, so `--entrypoint chown` answers *"executable file not
found in $PATH"*.

### It starts, then exits, and the log says the schema does not match

That is `schema_check` working. The volume is at a revision this image does not
know, or the image is ahead of the volume. Either run the image the volume
expects, or upgrade deliberately, see **Upgrading**. Do not reach for
`SPENDTRACKER_AUTO_MIGRATE=1` to make the message go away; it will migrate, and
you will not have a backup.

### Every request answers `400 Invalid host header`

The name in the address bar is not in `SPENDTRACKER_ALLOWED_HOSTS`. Add it
(see section 2) and restart. The check exists so that a page on somebody
else's domain cannot re-point its DNS at this machine and talk to the app as
though it were same-origin.

### Sign-in answers but you land back on the sign-in screen

The browser refused to store the `Secure` session cookie because the page is
plain HTTP at an address that is not `localhost`. Reach the app over HTTPS
(sections 2 and 3) or at `http://localhost:8848` (section 1). Nothing is
written to any log when this happens.

### The sidecar node never appears in the admin console (section 3)

`docker compose logs tailscale`. The usual causes, in order: the auth key was
pasted with a trailing space or quotes; the key was made without the tag, or
the tag is not in `tagOwners` yet; the key already expired before first use.
Fix `.env`, then `docker compose down -v && docker compose up -d`. `-v` is
safe only before the ledger has anything in it; afterwards remove just the
sidecar's state with `docker volume rm spend-tracker_ts-state` while stopped.

### A command printed nothing

Add `-T`. See the demo section.

---

## Not using Chainguard

The default base is Chainguard's Wolfi Python: minimal, and the runtime variant
carries no shell and no package manager. If you would rather not:

```bash
docker compose build \
  --build-arg PY_BASE=python:3.12-slim \
  --build-arg PY_RUN=python:3.12-slim
```
