# The pre-update hook (servers only)

A command of yours that the updater runs **on the host, before anything is
stopped**, every time the owner confirms an update. The motivating case is a
hypervisor snapshot of the machine Spend Tracker lives on, which nothing
inside a container can take.

**It is off unless you set it up, and its absence never blocks an update.**
Once set up, **a failing hook stops the update before it starts**: a non-zero
exit, a hook that runs past its timeout, or no runner answering at all. The
owner sees *Not started: the pre-update hook failed.*, the app keeps serving,
and the update's history holds the reason and the end of the command's output.

The personal-computer bundle has no hook.

## How it works

```
 updater container                      host
 ─────────────────                      ────
 /hook/<id>.request  ──── bind mount ──▶ /var/lib/spend-tracker/hook/<id>.request
                                              │ spend-tracker-hook.path sees it
                                              ▼
                                         spend-tracker-hook.service
                                           hook-runner: takes it (<id>.taken),
                                           checks it, runs
                                           /etc/spend-tracker/pre-update-hook
 /hook/<id>.result   ◀─────────────────  writes <id>.result (exit + output)
```

- The request names **versions only** (`from`, `to`, and the update's id).
  The runner never runs anything a request says: it runs the one command you
  installed, and passes the versions to it as `SPENDTRACKER_HOOK_FROM` and
  `SPENDTRACKER_HOOK_TO` (plus `SPENDTRACKER_HOOK_ID`), after checking each is
  `X.Y.Z`. A request that fails that check is refused, and the update does
  not start.
- One request is run **once**. The runner claims it by linking it to
  `<id>.taken` and removing the request, and never runs an id that already has
  a `.taken` or a `.result`.
- The updater waits up to `timeout_seconds` (from `hook.json`, default 300, at
  most 3600) for the result, and gives up after 60 seconds if nothing has
  picked the request up. Time the machine spends asleep or paused does not
  count.

## Install

As root, on the machine that runs the containers (not inside one). The runner
needs `python3` (3.9 or later, standard library only) and systemd.

```sh
# 1. The runner and the units, from a checkout of this repository.
install -D -m 0755 deploy/updater/host-hook/hook-runner /usr/local/lib/spend-tracker/hook-runner
install -m 0644 deploy/updater/host-hook/spend-tracker-hook.path    /etc/systemd/system/
install -m 0644 deploy/updater/host-hook/spend-tracker-hook.service /etc/systemd/system/

# 2. The directory the updater sees as /hook: root's, shared with the
#    updater's group (65532), setgid so every file in it keeps that group.
install -d -o root -g 65532 -m 2770 /var/lib/spend-tracker/hook

# 3. Your command. Root's, executable, not writable by anyone else, and a
#    file rather than a symlink -- the runner refuses anything else.
#    Start from the example, or write your own.
install -D -o root -g root -m 0700 your-command /etc/spend-tracker/pre-update-hook

# 4. Turn the hook on. Its presence is what "configured" means; without it
#    the updater skips the hook and says so.
echo '{"timeout_seconds": 300}' > /var/lib/spend-tracker/hook/hook.json
chmod 0644 /var/lib/spend-tracker/hook/hook.json

# 5. Start watching.
systemctl daemon-reload
systemctl enable --now spend-tracker-hook.path
```

Then mount the directory into the updater (next section) and recreate it:
`docker compose up -d updater`.

To turn the hook off without uninstalling it, remove `hook.json`.

### The mount: `compose.override.yaml`

The standard `compose.yaml` carries **no** hook mount, on purpose: under Podman
a bind mount whose source does not exist is an error, and most installations
have no hook. Add it on the server only, in a `compose.override.yaml` next to
`compose.yaml` (Compose reads it automatically; if you start the stack with
`-f`, name it too):

```yaml
# compose.override.yaml -- this server only.
services:
  updater:
    volumes:
      - /var/lib/spend-tracker/hook:/hook
```

Nothing else changes: the app never sees this directory.

**Rootless engines.** The steps above assume a rootful engine, where the
updater runs as uid and gid 65532. Under rootless Docker or rootless Podman
on Linux the updater is the engine's own user, so give that user the
directory instead (`install -d -o <user> -g <user> -m 0770 ...`); the runner
still runs as root from systemd and writes results any user can read inside
that directory.

### Check it

```sh
systemctl status spend-tracker-hook.path     # active (waiting)
journalctl -u spend-tracker-hook.service     # one line per request, and its exit
ls /var/lib/spend-tracker/hook               # <id>.taken and <id>.result per update
```

The `.taken` and `.result` files are the record of each run, and are small;
delete old ones whenever you like. Do not delete a `.taken` while an update is
in progress.

If `systemctl status spend-tracker-hook.path` ever shows
`trigger-limit-hit`, the runner failed before it could remove a request: the
journal for `spend-tracker-hook.service` says why. Remove the stray
`*.request` and `systemctl restart spend-tracker-hook.path`.

## The example: a Proxmox snapshot

> **This is an example, not part of the product.** Read it, change the names,
> and test it by hand before an update depends on it.

Spend Tracker runs in a VM on a Proxmox node. The hook runs **in the VM**, where
`qm` does not exist, so it asks the node to take the snapshot over SSH with a
key that can do nothing else.

On the **Proxmox node**, a forced command that accepts a version and nothing
more:

```sh
#!/bin/sh
# /usr/local/sbin/spend-tracker-snapshot  (root:root, 0755) -- EXAMPLE
set -eu
VMID=100                                  # the VM Spend Tracker runs in
v=${SSH_ORIGINAL_COMMAND:-}
case "$v" in
  ''|*[!0-9.]*) echo "refused: not a version" >&2; exit 2 ;;
esac
# A snapshot name starts with a letter and holds letters, digits, - and _.
name="spend-pre-$(printf %s "$v" | tr . _)-$(date +%Y%m%d%H%M)"
exec qm snapshot "$VMID" "$name" --description "Before Spend Tracker $v"
```

and, in the node's `/root/.ssh/authorized_keys`, the VM's key restricted to it:

```
restrict,command="/usr/local/sbin/spend-tracker-snapshot" ssh-ed25519 AAAA... spend-tracker-hook
```

In the **VM**, the key at `/etc/spend-tracker/pve-snapshot-key` (root, 0600,
no passphrase), the node in root's `known_hosts`, and
[`pre-update-hook.example`](pre-update-hook.example) installed as
`/etc/spend-tracker/pre-update-hook` with your node's name in it.

Try it by hand before relying on it:

```sh
sudo env SPENDTRACKER_HOOK_TO=0.0.0 /etc/spend-tracker/pre-update-hook && echo ok
```

The snapshot is taken while the app is still serving, without `--vmstate`, so
it is crash-consistent: what a power cut would have left. The ledger is SQLite
in WAL mode, which recovers from exactly that. The updater's own backup,
taken a moment later after the app stops, is still the one a rollback uses;
the snapshot is the step back from anything the updater cannot undo.
Snapshots accumulate: prune them on the node (`qm listsnapshot`, `qm
delsnapshot`) as you would any other.
