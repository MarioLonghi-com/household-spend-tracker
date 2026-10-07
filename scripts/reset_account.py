"""Reset an account from the server, or make somebody an owner.

    python -m scripts.reset_account you@example.com --password --authenticator
    python -m scripts.reset_account you@example.com --make-owner
    python -m scripts.reset_account you@example.com --enable
    python -m scripts.reset_account new@example.com --make-owner --name "Their Name" \\
        --household "Our household"

For when nobody can do it from a screen: the only owner is locked out or
disabled, or no owner who can sign in is left. An owner who can sign in does
all of this from *Admin* instead, where the audit log names them.

- `--password`, `--authenticator`, or both: a one-time reset link. Issuing it
  shuts the account at once, exactly as an owner's link does -- every session,
  trusted browser, agent key and passkey ends; the password is replaced with one nobody
  knows, and/or the authenticator and its recovery codes are cleared -- and
  following the link is the only way back in.
- `--make-owner`: makes an existing account an owner. On its own it prints no
  link, and the account's own password and authenticator go on working.
- `--enable`: enables an account an owner disabled, as *Admin* does. Nothing
  else here does: a disabled account stays disabled, cannot sign in, and a
  link for it will not open. On an account that is not disabled it writes
  nothing, and it does not let `--household` go ahead on its own. Refused
  beside `--name`, because a new account is not disabled. Enabling undoes no
  reset: an account an earlier link shut stays shut, and that link -- or, once
  it has lapsed or been withdrawn, a new one -- is the way in. The account
  shows a cleared authenticator, and the command says so; a replaced password
  looks like any other, so it cannot.
- `--make-owner --name NAME`, for an address with no account: creates it, as an
  owner, with no password anybody knows and no authenticator, and prints a
  link that sets both. Without `--make-owner` an unknown address is refused.
- `--household NAME_OR_ID`, repeatable: also puts the account in that
  household. Never implied, not even for a new owner, and refused on its own:
  a membership alone is an owner's act.

Everything is checked before it asks -- the address, every household, where
the link will point -- and a refusal writes nothing. Then it is one audited
batch, `kind=admin` with `source={"via": "scripts.reset_account"}`. Its actor
is the account acted on: `batches.actor_id` needs a real user and there is no
system account, the answer `scripts.reset_authenticator` and housekeeping give
too. The batch belongs to no household, so no household's *History* lists it;
the audit log has it, marked by that source as done from the server. A link
from here names no issuer (`account_resets.created_by_id` is null), which is
how the page behind it says "reset from the server".

The link is printed once, here: not to the log, and the audit log keeps only
its hash, redacted. It works once, for `account_resets.VALID_HOURS` hours. It
points at `--base-url`, else `SPENDTRACKER_PUBLIC_URL`; with neither it
refuses, because a link to a host nobody can reach is found out by the person
holding it. In a container:

    docker compose exec app python -m scripts.reset_account you@example.com --password

`exec`, not `run -T`: it asks before it writes. `--yes` does not ask.

`scripts.reset_authenticator` stays the route for when no browser can reach
the app: it enrols the new authenticator here, in the terminal, with no link.
"""

from __future__ import annotations

import argparse
import sys

#: What every batch this writes carries, so the audit log tells it from an
#: owner's act and from `scripts.reset_authenticator`. The batch has no
#: household, so this is the only mark of it: no household's History lists it.
SOURCE = {"via": "scripts.reset_account"}

#: `users.display_name` is a String(120).
NAME_MAX = 120


class _Refused(Exception):
    """A reason to stop before anything is written. The message is the sentence."""


def main(argv: list[str] | None = None, *, ask=input) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("email", help="the address the account signs in with")
    parser.add_argument(
        "--password", action="store_true", help="the link sets a new password; the old one stops now"
    )
    parser.add_argument(
        "--authenticator",
        action="store_true",
        help="the link enrols a new authenticator; the old one and its recovery codes stop now",
    )
    parser.add_argument(
        "--make-owner",
        action="store_true",
        help="make the account an owner; with --name, create it when the address has none",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="enable the account if an owner disabled it, so it can sign in and a link for it opens",
    )
    parser.add_argument("--name", help="a new owner's display name, for an address with no account")
    parser.add_argument(
        "--household",
        action="append",
        default=[],
        metavar="NAME_OR_ID",
        help="also put the account in this household; repeat for more",
    )
    parser.add_argument(
        "--base-url",
        help="where people open the app, such as https://spend.example.ts.net "
        "(default: SPENDTRACKER_PUBLIC_URL)",
    )
    parser.add_argument("--yes", action="store_true", help="do not ask before writing")
    args = parser.parse_args(argv)

    try:
        return _run(args, ask=ask)
    except _Refused as refused:
        print(f"{refused} Nothing was changed.", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, *, ask) -> int:
    if not (args.password or args.authenticator or args.make_owner or args.enable):
        # `--household` alone is refused too: a membership on its own is an
        # owner's act. `--enable` that finds nothing to enable does not change
        # that: it is refused again below, once the account has been read.
        raise _Refused(
            "Say what to do: --password, --authenticator, --make-owner, --enable, or more than one."
        )
    if args.name is not None and not args.make_owner:
        raise _Refused("--name creates an owner, so it goes with --make-owner.")

    from sqlalchemy import select, text

    from app import config
    from app.audit.batch import batch
    from app.auth import setup
    from app.auth.email_canonical import canonical
    from app.db import session_scope
    from app.errors import ValidationError
    from app.models import AccountReset, BatchKind, HouseholdMember, Role, User, utcnow
    from app.services import account_resets
    from app.services import households as household_service
    from app.services import users as user_service

    try:
        folded = canonical(args.email)
    except ValidationError as exc:
        raise _Refused(f"That address will not do: {exc}.") from exc

    with session_scope() as session:
        user = session.scalars(select(User).where(User.email_canonical == folded)).one_or_none()
        name = (args.name or "").strip()
        creating = user is None
        if creating:
            if not args.make_owner:
                raise _Refused(
                    f"No account signs in as {args.email}. To create one as an owner, add "
                    '--make-owner --name "Their name".'
                )
            if args.enable:
                raise _Refused(
                    f"No account signs in as {args.email}, and a new one is not disabled, so "
                    "--enable has nothing to do. Leave it out."
                )
            if not name:
                raise _Refused(f"No account signs in as {args.email}. To create one, give --name.")
            if len(name) > NAME_MAX:
                raise _Refused(f"A name is at most {NAME_MAX} characters.")
            if not setup.is_configured(session):
                raise _Refused(
                    "This instance has not been set up. Its first owner comes from the setup "
                    "screen, with the token the app prints when it starts."
                )
        elif args.name is not None:
            # Folding can match an address that was typed differently: a
            # dotted Gmail address is the undotted one. Somebody who meant to
            # create a new person must not end up acting on an old one.
            raise _Refused(
                f"{args.email} already signs in as {user.display_name} <{user.email}>, and "
                "--name is only for a new account. Leave it out to act on theirs."
            )

        houses = _households(session, args.household)

        reset_password = creating or args.password
        reset_authenticator = creating or args.authenticator
        issuing = reset_password or reset_authenticator
        base = _base_url(args.base_url, config.settings.public_url) if issuing else None

        promoting = args.make_owner and (creating or user.role is not Role.owner)
        disabled = not creating and user.disabled_at is not None
        enabling = args.enable and disabled
        joining = [
            house for house in houses
            if creating or not household_service.is_member(session, house.id, user.id)
        ]
        staying = [house for house in houses if house not in joining]
        pending = None if creating else session.scalars(
            select(AccountReset).where(AccountReset.user_id == user.id)
        ).one_or_none()
        # Shut, with nothing waiting to open it: what an earlier link leaves
        # once it is withdrawn, or lapses and housekeeping sweeps it. Each half
        # shows: a cleared password and a cleared authenticator are both NULL.
        missing = [] if creating else [
            what for what, gone in (
                ("password", user.password_hash is None), ("authenticator", user.totp_secret is None)
            ) if gone
        ]
        no_way_in = not issuing and pending is None and bool(missing)
        lacks = " and ".join(f"no {what}" for what in missing)
        fixes = " ".join(f"--{what}" for what in missing)

        who = f"{name} <{args.email.strip()}>" if creating else f"{user.display_name} <{user.email}>"
        if joining and not (promoting or enabling or issuing or args.make_owner):
            # Past the first check on --enable alone, and the account is not
            # disabled: what is left is --household on its own, refused there.
            raise _Refused(
                f"{who} is not disabled, so --enable has nothing to do, and --household does not "
                "go ahead on its own: a membership alone is an owner's act."
            )
        if not (promoting or enabling or issuing or joining):
            # Only --make-owner and --enable can each come to nothing.
            already = " and ".join(
                what for what, asked in (("an owner", args.make_owner), ("enabled", args.enable)) if asked
            )
            also = ", and in every household named" if staying else ""
            print(f"{who} is already {already}{also}. Nothing to do; nothing was changed.")
            if disabled:
                print("The account is disabled, though, and cannot sign in: --enable enables it.")
            if no_way_in:
                print(f"It has {lacks}, though, and no reset link is waiting, so it cannot")
                print(f"sign in: {fixes} issues a link that sets it.")
            return 0

        print(f"A new account, {who}:" if creating else f"For {who}:")
        if creating:
            print("  - create it, as an owner, with no password and no authenticator")
        elif promoting:
            print("  - make them an owner")
        elif args.make_owner:
            print("  - (already an owner)")
        if enabling:
            if issuing:
                so = ", so the link opens"
            elif pending is not None:
                so = ", so the link already waiting for them opens, if it has not lapsed"
            else:
                so = "; their password and authenticator are left as they are"
            print(f"  - enable them{so}")
        elif args.enable:
            print("  - (already enabled)")
        if issuing and not creating:
            print("  - end every session, trusted browser, agent key and passkey they have")
            if reset_password:
                print("  - clear their password, so it no longer signs them in")
            if reset_authenticator:
                print("  - clear their authenticator and delete their recovery codes")
            if pending is not None:
                print("  - replace the link already waiting for them; the new one also covers")
                print("    whatever that one reset")
        if issuing:
            print(f"  - print a one-time link, good for {account_resets.VALID_HOURS} hours, "
                  + ("for them to choose both" if creating else "for them to set new ones"))
        for house in joining:
            print(f"  - add them to the household {house.name}")
        for house in staying:
            print(f"  (already in {house.name}; left as it is)")
        if disabled and not enabling:
            print()
            print("This account is disabled, and it stays disabled: it cannot sign in, and a")
            print("link for it will not open, until it is enabled. --enable does that here.")
        if no_way_in:
            print()
            print(f"This account has {lacks}, and no reset link is waiting to set it,")
            print(f"so it cannot sign in, enabled or not. {fixes} issues a link that does.")
        print()
        if not args.yes and ask("Type 'yes' to go ahead: ").strip().lower() != "yes":
            print("Nothing was changed.")
            return 1

        if creating:
            user = User(
                email=args.email.strip(),
                email_canonical=folded,
                display_name=name,
                # NULL, as a reset leaves it: the link below sets both.
                password_hash=None,
                role=Role.owner,
                totp_secret=None,
            )
            # The new owner is the actor of the batch that creates them, as the
            # setup wizard's first owner is, so the batch row names a user that
            # is inserted after it in the same transaction.
            session.execute(text("PRAGMA defer_foreign_keys=ON"))

        reset = token = None
        # own_transaction=False: one transaction, all or nothing. Creating needs
        # it (see above), and a failure then leaves no half-made owner.
        with batch(
            session, kind=BatchKind.admin, actor_id=user.id, source=dict(SOURCE), own_transaction=False
        ):
            if creating:
                session.add(user)
                session.flush()
            elif promoting:
                user.role = Role.owner
            if enabling:
                user_service.set_disabled(session, user=user, disabled=False, by=None)
            for house in joining:
                session.add(
                    HouseholdMember(
                        household_id=house.id, user_id=user.id, added_by_id=None, added_at=utcnow()
                    )
                )
            if issuing:
                reset, token = account_resets.issue(
                    session,
                    user,
                    password=reset_password,
                    authenticator=reset_authenticator,
                    by=None,
                )
            session.flush()
        session.commit()

    print()
    print("Done.")
    if creating:
        print(f"  created     {who}, an owner")
    elif promoting:
        print(f"  made owner  {who}")
    if enabling:
        print(f"  re-enabled  {who}")
    for house in joining:
        print(f"  added to    {house.name}")
    if token is not None:
        sets = " and ".join(
            what for what, on in (("a new password", reset.password), ("a new authenticator", reset.authenticator))
            if on
        )
        print()
        print(f"The link sets {sets}. It is shown here once and kept nowhere;")
        print("send it by a route you trust, because whoever holds it can use it:")
        print()
        print(f"  {account_resets.link_for(token, base=base)}")
        print()
        print(f"It works once, for {account_resets.VALID_HOURS} hours: until "
              f"{reset.expires_at:%Y-%m-%d %H:%M} UTC.")
        print("Until it is followed, nobody can sign in to this account.")
        if disabled and not enabling:
            print("It will not open while the account is disabled; --enable enables it.")
    elif disabled and not enabling:
        print("  Still disabled: nobody can sign in to this account until it is enabled.")
    elif pending is not None:
        # An earlier link shut the account; enabling or promoting does not undo that.
        print("  The link already waiting for them is still the only way in. If it has")
        print("  lapsed, --password or --authenticator issues a new one.")
    elif no_way_in:
        print(f"  They still cannot sign in: the account has {lacks}, and no link")
        print(f"  is waiting to set it. {fixes} issues one.")
    elif enabling or promoting:
        print("  Their own password and authenticator sign them in, as before.")
    print()
    print("It is in the audit log as done from the server, not by any owner.")
    return 0


def _households(session, wanted: list[str]) -> list:
    """Each `--household`, by id or by name, or a refusal naming the choices.

    By id first, then by name ignoring case. Two households can share a name;
    then the name is refused and the ids are printed, rather than one of them
    picked.
    """
    from sqlalchemy import select

    from app.models import Household

    if not wanted:
        return []
    every = list(session.scalars(select(Household).order_by(Household.created_at)))
    found: list = []
    for value in wanted:
        value = value.strip()
        matches = [house for house in every if house.id == value] or [
            house for house in every if house.name.casefold() == value.casefold()
        ]
        if not matches:
            listed = ", ".join(f"{house.name} ({house.id})" for house in every) or "none"
            raise _Refused(f"No household is called or numbered {value!r}. The households here: {listed}.")
        if len(matches) > 1:
            listed = ", ".join(house.id for house in matches)
            raise _Refused(
                f"{len(matches)} households are called {value!r}: {listed}. Name the one you mean by its id."
            )
        if matches[0] not in found:
            found.append(matches[0])
    return found


def _base_url(given: str | None, configured: str) -> str:
    """Where the link points: `--base-url`, else `SPENDTRACKER_PUBLIC_URL`."""
    from app import config

    if given is not None:
        try:
            checked = config._public_url(given)
        except ValueError:
            checked = ""
        if not checked:
            raise _Refused(f"--base-url must be an origin such as https://spend.example.ts.net, not {given!r}.")
        return checked
    if configured:
        return configured
    raise _Refused(
        "There is no address to put in the link. Pass --base-url with the address people "
        "open the app at, such as https://spend.example.ts.net, or set SPENDTRACKER_PUBLIC_URL."
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
