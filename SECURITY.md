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
the statement parsing library, and the `/db` viewer.

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
