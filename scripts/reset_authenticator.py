"""Give a member a new authenticator from the server, when no screen can.

    python -m scripts.reset_authenticator you@example.com

For a member with neither their authenticator nor an unused recovery code, or
for every member at once after `secret.key` was lost and the app made a new
one. The screens cover everyone else: a recovery code at sign-in, then *your
name -> Authenticator -> Set up a new authenticator*.

What it does, in order, and nothing is written until step 3:

1. Prints a new secret as an `otpauth://` link and as text, for the member to
   add to their authenticator app.
2. Asks for a code from it. Three tries; a wrong clock is the usual cause.
3. In one audited batch: stores the new secret, sealed with the `secret.key`
   in the data directory now; replaces the recovery codes with ten new ones;
   ends every session and trusted browser, revokes every live agent key and
   removes every passkey (#121): a passkey is a way in on its own.
4. Prints the ten recovery codes, once.

The new secret and the codes go to this terminal and nowhere else: not the
log, not the audit log (`code_hash` and `totp_secret` are redacted there).
Run it with the member beside you, or for yourself.

It can run while the app is up: it is one short write transaction, like any
request. In a container:

    docker compose exec app python -m scripts.reset_authenticator you@example.com

`exec`, not `run -T`: this one asks a question, so it wants the terminal.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None, *, ask=input) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("email", help="the address the member signs in with")
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from app.audit.batch import batch
    from app.auth import totp
    from app.auth.email_canonical import canonical
    from app.db import session_scope
    from app.models import BatchKind, User
    from app.services import profile

    with session_scope() as session:
        user = session.scalars(
            select(User).where(User.email_canonical == canonical(args.email))
        ).one_or_none()
        if user is None:
            print(f"No member signs in as {args.email}. Nothing was changed.", file=sys.stderr)
            return 2

        secret = totp.new_secret()
        print(f"A new authenticator for {user.display_name} <{user.email}>.")
        print()
        print("Add it to the authenticator app: scan or paste this link,")
        print(f"  {totp.provisioning_uri(secret, email=user.email)}")
        print("or type this key in by hand:")
        print(f"  {secret}")
        print()
        if user.disabled_at is not None:
            print("This member is disabled. The authenticator is replaced anyway; an")
            print("owner still has to enable them before they can sign in.")
            print()

        step = None
        for _ in range(3):
            step = totp.code_matches(secret, ask("A code from it, to prove it works: ").strip())
            if step is not None:
                break
            print("That code is not right. Check the time on the device and try again.")
        if step is None:
            print("Nothing was changed. The authenticator just added will not work; remove it.")
            return 1

        with batch(
            session,
            kind=BatchKind.admin,
            actor_id=user.id,
            source={"via": "scripts.reset_authenticator"},
        ):
            done = profile.reset_authenticator_from_the_server(session, user, secret=secret, step=step)

    print()
    print("Done. The new authenticator is the only one that works now.")
    print(f"  sessions ended      {done.sessions_ended}")
    print(f"  browsers forgotten  {done.devices_revoked}")
    print(f"  agent keys revoked  {done.keys_revoked}")
    print(f"  passkeys removed    {done.passkeys_removed}")
    print()
    print("New recovery codes. Each works once, in place of a code. They are not")
    print("shown again; store them somewhere that is not this terminal:")
    for code in done.recovery_codes:
        print(f"  {code}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
