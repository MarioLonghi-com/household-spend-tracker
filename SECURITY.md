# Security

Spend Tracker holds financial records and the credentials that reach them. If
you find something wrong with how it does that, please tell me before you tell
anyone else.

## Reporting

Open a [private security advisory](https://github.com/MarioLonghi-com/household-spend-tracker/security/advisories/new)
on this repository. That channel is private between you and the maintainer.

If advisories are unavailable to you, email **info@mariolonghi.com** with
`household-spend-tracker security` in the subject.

Please do not open a public issue for a vulnerability.

Expect an acknowledgement within a week. This is a one-person project and not a
funded one, so there is no bounty and no guaranteed turnaround beyond that.

## What is in scope

The application in this repository: authentication and the second factor, the
session and trusted-device model, household isolation, the audit log and undo,
the statement parsing library, the `/db` viewer, and the self-updater: its
container, its image verification, the recovery page and the release zip's
launchers.

Two things are **not** vulnerabilities here, because they are deliberate
design:

- **There is no public-internet threat model.** This is designed to run on a
  [Tailscale](https://tailscale.com) tailnet behind `tailscale serve`, never on
  Funnel and never on the open internet. Findings that require an
  unauthenticated stranger to reach the port describe a deployment that is not
  the supported one.
- **The owner can read the whole database.** `/db` serves a redacted snapshot
  to the instance owner by design. An owner seeing another household's rows is
  expected; a *member* seeing them is not, and that is in scope.

## Self-update and the engine socket

A container install updates itself from the browser
([`deploy/UPGRADING.md`](deploy/UPGRADING.md#from-the-browser-container-installs)).
That takes the container engine's API socket, and **the socket is root on
the machine**: anything that can ask the engine to create a container can
start one with the host's files mounted. Under a rootless engine it is the
whole of that user's account instead. Everything below follows from that.

**Only the updater holds it. The app never does.** The socket is mounted into
one container, `updater`, which listens on nothing, is never in the Tailscale
sidecar's network, runs with a read-only root, no capabilities and
`no-new-privileges`, and is never root on the host: uid 65532 with the
socket's group, or, under a rootless engine, in-container uid 0, which is the
unprivileged user who owns that engine. `deploy/tailnet/check.sh` fails if
any other container of the project has the socket mounted. The app and the
updater share one small volume and talk only through files in it.

**What the app can make the updater do** is a short, fixed list of requests,
each with fixed fields, validated by the updater as if the app were hostile:

- *prepare* and *apply* a **newer, published release of this application**,
  named by its version: never an image, a path, a command or a mount, never
  an older release, never a prerelease, and never while the app runs an image
  built locally;
- *discard* a prepared update;
- replace the updater with **the updater of this release or a newer one**;
- the recovery page's requests, each of which carries the recovery code.

It cannot make the updater run anything else. The updater's own engine
client is a list of calls, checked before a byte reaches the socket and held
by a test: containers of its own compose project only; images of this
repository's two packages only, by digest; never the sidecar except to read
it and run its health check; no build, no volume or network calls, no prune;
and the one setting of an existing container it changes is the app's restart
policy (`no` while it is parked as `-previous`, its own again on a rollback),
in a request that may carry nothing else.
Every container it creates is refused if it asks for `Privileged`, added
capabilities, the host's PID, IPC, UTS, user or network namespace, devices,
or any host path other than the socket and the compose directory the updater
itself was started with (plus the pre-update hook's directory, on a server
that set one up).

The step-up an owner gives to *Update* (password and code) protects the
owner's intent, not the machine: none of the updater's guarantees depends on
the app having checked anything.

**Every image is verified before it is pulled**, the app's and the updater's
alike, in `updater/verify.py`, with sigstore-python. The release's build
attestation, read from the registry beside the image, must carry a valid
signature, certificate chain and Rekor inclusion proof, and must say: issued
by GitHub Actions; signed by this repository's
`.github/workflows/release.yml` at `refs/tags/vX.Y.Z`, the release being
installed; this repository and its owner **by numeric id**, which a rename
cannot take over; a GitHub-hosted runner, triggered by a push; and a subject
that is exactly the digest about to be pulled, named as the package it is
pulled from, so an app attestation cannot pass for the updater's. A version
with any prerelease suffix is refused. After the pull, the image's version
and commit labels must agree with the attestation, or the image is deleted.
The trust root comes from Sigstore's TUF repository when it can be reached;
otherwise the copy embedded in the updater image (`updater/trusted_root.json`)
is used to verify, and a refusal under the fetched root is final. **There is
no switch, variable or argument that skips verification.** The end-to-end
job in CI, whose images cannot carry a release attestation, swaps in a
test-only policy from `tests/`, and a CI check keeps that out of `updater/`.

**The recovery code** opens the recovery page, which appears only when an
update could not undo itself and nothing serves the ledger. It is made by the
server for one update, 140 bits shown as seven groups of four characters,
and shown once. The app holds only its `scrypt` hash, in memory, for ten
minutes, bound to that prepared update and that owner, and sends the hash in
the update request. The updater checks every code; the recovery page, which
runs the previous app image with no socket and the ledger mounted read-only,
only forwards it, and the request file that carries it is deleted the moment
the updater takes it. The code works only until that update has finished,
when the hash is deleted. Five wrong codes refuse every code for 15 minutes,
doubling after each further five; while that pause lasts, codes are refused
unread, so a flood of requests cannot lengthen it. The page is reachable only where the app was: `localhost` on a
personal computer, the tailnet on a server.

**What a compromised app can still do.** It already holds the ledger and
`secret.key`; what it must not gain is the machine. The files the app and the
updater share are group-writable by design, so an app under someone else's
control could forge the updater's handover files, its lock or its heartbeat.
The most that buys is a **delay or a forced take-back**: an updater stepping
aside early, or taking back over from its successor, and updates not
happening until an owner notices. It can never get unverified code run: an
updater checks its successor's image with the engine, at the verified digest,
before it starts it. The same holds for every request: at worst the app makes
the updater install a genuine, newer release of this application, built by
this repository's release workflow.

**A suggested egress firewall.** Not required, and not set up by anything
here. The updater container reaches out only while it handles a request an
owner started, and only to:

| Host | For |
|---|---|
| `ghcr.io` | resolving a release to a digest, and its attestation |
| `pkg-containers.githubusercontent.com` | where ghcr.io redirects blob downloads, the attestation included |
| `tuf-repo-cdn.sigstore.dev` | refreshing Sigstore's trust root; without it the embedded root is used |

The engine itself pulls the images, from `ghcr.io` and
`pkg-containers.githubusercontent.com`, and the app reaches `api.github.com`
for *Check the repository* and `api.ynab.com` for the one-time YNAB import.
A host firewall that lets the compose project's network reach those and
nothing else costs the updater nothing. In the root `compose.yaml` the app
shares that network, so allow the app's two hosts as well.

## What the instance holds, and what it costs to lose

All of it is in the data directory: `~/.local/share/spend-tracker` by default,
`/var/lib/spend-tracker` in the container, or wherever `SPENDTRACKER_DATA_DIR`
points. `make upgrade-check` prints the one it resolved.

| | |
|---|---|
| `spendtracker.sqlite3` | the ledger, argon2id password hashes, sealed TOTP secrets |
| `secret.key` | the root key the TOTP secrets are sealed with |
| `snapshot.sqlite3` | a redacted copy, only if `make snapshot` was run |

Losing `secret.key` loses no data, and on its own it signs nobody out:
sessions and trusted devices are random values checked by database lookup,
not signed tokens. What it costs is every authenticator. The app runs in
**recovery mode** ([TROUBLESHOOTING](deploy/TROUBLESHOOTING.md#4-secretkey-is-lost-or-wrong)),
and every member the key cannot open signs in with one of their recovery
codes -- a trusted browser does not skip that -- and sets up a new
authenticator. That recovery code does what one always does: it signs the
member out everywhere, forgets their trusted browsers and revokes their live
agent keys. Putting the original key back ends recovery mode for everybody
who has not re-enrolled, but it brings back nothing a recovery code has
already revoked, so a key that was only misplaced is worth finding before
anybody signs in.

So it is not a backup you can skip. And if an older install still keeps its
data in the checkout's own `./data`, that directory is gitignored, so `git
clean -xdf` takes the database *and* the key; the default location is outside
the checkout for exactly that reason.

## Supported versions

The newest release and the tip of `main`. Fixes are not backported to older
releases; upgrade instead ([`deploy/UPGRADING.md`](deploy/UPGRADING.md)).
