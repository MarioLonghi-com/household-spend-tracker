"""Put a backup back, and be loud about what that costs.

    python -m scripts.restore backups/20260923-024814
    python -m scripts.restore spendtracker-20260925T112800Z.zip
    python -m scripts.restore spendtracker-20260925T112800Z.sqlite3 --key PATH

Three shapes, because the app makes two of them: `make backup` writes a folder
with the key in it, and the Application screen writes a bare `.sqlite3` and
downloads it as a zip, with the key only when the person asked (#133).

The half nobody rehearses. A restore procedure that has never been executed is
a document rather than a capability, and the number that decides during a real
incident whether restoring is even on the table -- how long it takes -- is only
knowable by having done it once.

## What this does

1. Verifies the backup **before** touching anything live: reopens it, reads its
   `alembic_version` and counts its rows.
2. Settles on a key, and **opens a real sealed TOTP secret in the backup with
   it**. The key is the one in the backup; else `--key`; else the one already
   in the data directory, which is right when restoring onto the instance the
   backup came from. A key that cannot open it is refused unless
   `--without-key` says the person knows every authenticator will be refused.
3. Moves the current database -- and the key, if it is being replaced -- aside
   rather than overwriting them. A restore is itself a thing that can be the
   mistake, and the state you were in thirty seconds ago is worth a rename.
4. Copies the backup into place, with the key.
5. Says which revision the restored database is at, and therefore which code
   will boot against it.

## What it refuses

**It will not run while the port is answering.** Copying a SQLite file out from
under a live process is how a WAL and its database stop agreeing.

> That check sees its **own** network namespace. Run from a one-off container
> -- `docker compose run` -- it cannot see the app container at all and will
> find the port free however busy the real one is. In Docker, stop the service
> first (`docker compose stop app`) and do not rely on this to notice.

**It does not choose the code for you.** The restored file is stamped at the
revision it was taken at, and `app/schema_check.py` refuses to boot code that
disagrees with it -- which is the guard working, not a fault. The revision is
printed for exactly that reason: it tells you which tag to check out.

## The WAL

The live `-wal` and `-shm` files are moved aside with the database and not
copied back. A `VACUUM INTO` copy is already consistent and has no WAL of its
own; leaving a stale one beside it would be a WAL belonging to a different
database, which SQLite is entitled to believe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import socket
import sys

#: Moved aside with the database. `-shm` is shared memory and `-wal` is the
#: write-ahead log; both belong to the file being replaced and neither is
#: meaningful beside a different one.
SIDECARS = ("-wal", "-shm")


def _port_is_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _key_verdict(database: pathlib.Path, key_text: str | None) -> tuple[bool, str]:
    """Does `key_text` open an authenticator secret in `database`? And a sentence.

    True with nothing to check is still True: a ledger nobody has enrolled in
    has no secret a wrong key could lock away.
    """
    from app.auth import crypto
    from app.services import backup_bundle

    sealed = backup_bundle.a_sealed_secret(database)
    if sealed is None:
        return True, "nobody has enrolled an authenticator in it, so any key will do"
    if key_text is None:
        return False, "there is no key at all to open its authenticator secrets with"
    user_id, secret = sealed
    if crypto.key_opens_totp_secret(key_text, secret, user_id=user_id):
        return True, "the key opens a real authenticator secret in it"
    return False, "the key does not open the authenticator secrets in it"


def _materialise(source: pathlib.Path, scratch: pathlib.Path) -> pathlib.Path:
    """The backup as a folder `verify` can read, whatever shape it arrived in.

    A folder is used where it is. A zip is unpacked -- only its four known
    names, see `backup_bundle.unpack` -- and a bare `.sqlite3` is copied in,
    both into a private scratch directory that is removed afterwards.
    """
    from app.permissions import copy_private
    from app.services import backup_bundle

    if source.is_dir():
        return source
    if not source.is_file():
        raise SystemExit(f"{source} is not there")
    if source.suffix == ".zip":
        try:
            return backup_bundle.unpack(source, scratch)
        except backup_bundle.BundleRefused as refused:
            raise SystemExit(str(refused)) from refused
    if source.suffix == ".sqlite3":
        copy_private(source, scratch / backup_bundle.DATABASE)
        return scratch
    raise SystemExit(
        f"{source} is not a backup this knows: give it a folder made by `make backup`, "
        "a .zip downloaded from the Application screen, or a .sqlite3 from its backups "
        "directory"
    )


def restore(
    source: pathlib.Path,
    *,
    port: int,
    yes: bool,
    key_file: pathlib.Path | None = None,
    without_key: bool = False,
) -> int:
    import tempfile

    from app.config import settings
    from app.permissions import private_dir

    if not settings.database_url.startswith("sqlite:"):
        raise SystemExit("this restores a SQLite file; a Postgres deployment uses pg_restore")

    if _port_is_busy(port):
        raise SystemExit(
            f"something is answering on port {port}. Stop the service first: copying a "
            "SQLite file out from under a live process is how a WAL and its database "
            "stop agreeing."
        )

    if key_file is not None and not key_file.is_file():
        raise SystemExit(f"--key {key_file} is not a file")

    private_dir(settings.data_dir)
    # In the data directory rather than /tmp: the same filesystem as the ledger,
    # already private, and in the container /tmp is a tmpfs -- memory -- which
    # is the wrong place to unpack a 725 MiB database.
    with tempfile.TemporaryDirectory(dir=settings.data_dir, prefix=".restore-") as scratch:
        return _restore(
            _materialise(source, pathlib.Path(scratch)),
            shown=source,
            port=port,
            yes=yes,
            key_file=key_file,
            without_key=without_key,
        )


def _restore(
    folder: pathlib.Path,
    *,
    shown: pathlib.Path,
    port: int,
    yes: bool,
    key_file: pathlib.Path | None,
    without_key: bool,
) -> int:
    from app.config import settings
    from app.permissions import copy_private, private_dir
    from scripts import backup as backup_script
    from scripts import in_a_container

    checked = backup_script.verify(folder, require_key=False)
    manifest = {}
    if (folder / "manifest.json").exists():
        manifest = json.loads((folder / "manifest.json").read_text())

    live = pathlib.Path(settings.database_url.split("///", 1)[-1]).resolve()
    key = settings.data_dir / "secret.key"

    # Which key goes beside the restored ledger. The backup's own wins; then
    # the one named on the command line; then the one already here, kept in
    # place. `settings.secret_key` is deliberately not consulted for the last:
    # an instance keyed by SPENDTRACKER_SECRET_KEY keeps using that whatever
    # file this leaves behind, so it is the environment that has to be right.
    inside = folder / "secret.key"
    if inside.exists():
        chosen, key_from = inside, "the backup's own"
    elif key_file is not None:
        chosen, key_from = key_file, f"from {key_file}"
    elif key.exists():
        chosen, key_from = None, "the one already here, kept in place"
    else:
        chosen, key_from = None, "none"
    key_text = (chosen or key).read_text().strip() if (chosen or key).exists() else None
    opens, verdict = _key_verdict(folder / "spendtracker.sqlite3", key_text)

    print(f"Restoring {shown}")
    print(f"  taken      {manifest.get('taken_at', 'unknown')}")
    print(f"  version    {manifest.get('app_version') or manifest.get('packaged_by_version', 'unknown')}")
    print(f"  revision   {checked['revision']}")
    for table, count in sorted(checked["rows"].items()):
        print(f"  {count:>9,}  {table}")
    print()
    print(f"  over       {live}")
    print(f"  key        {key_from}: {verdict}")
    # Passkeys come back with the rows, but each works only for the host name
    # it was made under (#47 §1.2): restoring onto another one strands them.
    from app.auth import passkeys

    for line in passkeys.stranded(passkeys.hosts_in(folder / "spendtracker.sqlite3"), settings.rp_id):
        print(f"  passkeys   {line}; those members sign in with password + code and register again")
    print("  The database that is there now is moved aside, not deleted.")
    print()

    if not opens and not without_key:
        raise SystemExit(
            "Refusing: restored like this, every authenticator would be refused and "
            "nobody could sign in with theirs. Give the backup's key with --key PATH "
            "(KEY= with make). If that key is lost, --without-key restores anyway -- "
            "every member will then have to be given a new authenticator."
        )
    if not opens:
        print("  --without-key: restoring anyway. Every authenticator will be refused.")
        print()

    if not yes and input("Type 'restore' to go ahead: ").strip() != "restore":
        print("Nothing was changed.")
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    replacing = [live, *(live.with_name(live.name + tail) for tail in SIDECARS)]
    if chosen is not None:
        replacing.append(key)
    moved: list[tuple[pathlib.Path, pathlib.Path]] = []
    for path in replacing:
        if path.exists():
            aside = path.with_name(f"{path.name}.before-restore-{stamp}")
            path.rename(aside)
            moved.append((path, aside))
            print(f"  moved aside  {aside.name}")

    try:
        private_dir(live.parent)
        # Both through a 0600 open, never `shutil.copy2`, which creates the
        # file under the umask and copies the mode across afterwards -- the
        # ledger and the key each existed for a moment readable by others.
        copy_private(folder / "spendtracker.sqlite3", live)
        if chosen is not None:
            copy_private(chosen, key)
    except Exception:
        # Put it back exactly as it was. A restore that fails halfway and
        # leaves neither the old database nor the new one is the one outcome
        # worse than not restoring.
        for path, aside in moved:
            if path.exists():
                path.unlink()
            aside.rename(path)
        print("The copy failed. Everything has been put back as it was.")
        raise

    print()
    print(f"Restored. The database is at revision {checked['revision']}.")
    print("  1. Run the code that matches that revision, or newer and then")
    print("     `make migrate`. `schema_check` refuses to boot anything else, and")
    print("     says which revision it wanted.")
    if in_a_container():
        # `make serve` is not a command that exists in the image, and pointing
        # somebody at it is worse than saying nothing.
        print("  2. Start the service.        docker compose up -d")
        print("  3. Check it.                 curl -s localhost:8848/api/health")
    else:
        print(f"  2. Start the service.        make serve PORT={port}")
        print(f"  3. Check it.                 curl -s localhost:{port}/api/health")
    print("  4. Sign in. If the authenticator is refused, the wrong secret.key is in")
    print("     place -- it is the one thing a restore cannot recover from elsewhere.")
    print()
    print("  What was there before is beside it, renamed `.before-restore-" + stamp + "`.")
    print("  Remove those once you are sure, and not before.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "source",
        type=pathlib.Path,
        help="a folder made by scripts.backup, a .zip from the Application screen, "
        "or a .sqlite3 from its backups directory",
    )
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--yes", action="store_true", help="do not ask")
    parser.add_argument(
        "--key",
        type=pathlib.Path,
        metavar="PATH",
        help="the secret.key to restore with, when the backup does not carry one",
    )
    parser.add_argument(
        "--without-key",
        action="store_true",
        help="restore even though no key opens its authenticator secrets",
    )
    args = parser.parse_args(argv)
    return restore(
        args.source, port=args.port, yes=args.yes, key_file=args.key, without_key=args.without_key
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
