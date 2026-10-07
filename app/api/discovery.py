"""What an agent may learn before anybody has given it a key.

> [!important] The line this file draws
> Everything here is true of **the software**. Nothing here is true of *this
> deployment*: not a household, not an account, not an id, and — the part that
> matters — **not the route inventory**.

That distinction is the whole argument for serving this anonymously while
`/api/openapi.json` stays shut to the same caller.

An OpenAPI document is *generated*: it exposes whatever routes happen to
exist, including ones added next year by somebody who never thought about who
could read them. This file is *written*: every line in it was decided. That is
the same rule the rest of this build already keeps — `EXPECTED_EXCLUDED` makes
you classify a new table, `scripts/db_view.py` refuses a snapshot until you
have, and `Base.__init_subclass__` will not create a model that has not said
whether it is audited. A generated dump is the opposite of that principle; a
curated page is the principle.

It is **rendered from the same constants the API enforces**, never typed out
twice, for the reason the manifest's rate limit is: a published number nobody
checks is a docstring pretending to be a promise.
"""

from __future__ import annotations

from .. import __version__
from ..services import agent_keys, agent_requests
from ..services.platform import REPOSITORY
from .routers.agent import API_VERSION, CONVENTIONS, WELL_KNOWN


def descriptor(*, base_url: str = "") -> dict:
    """The same answer as `document`, for a caller that wanted JSON.

    Every line here is already on the prose page, and both are rendered from
    the same constants -- so the two cannot drift, and neither says anything
    about *this* deployment. Same anonymity argument as `document`: this is
    true of the software, names no household, no account, no id, and no route
    beyond the two entry points a caller needs to get started.

    Deliberately NOT the endpoint inventory. That is the manifest's job, the
    manifest needs a key, and the reasoning at the top of this file is why.
    """
    root = base_url.rstrip("/")
    return {
        "name": "Spend Tracker",
        "version": __version__,
        "api_version": API_VERSION,
        "base_url": root or None,
        "api_base": f"{root}/api/agent/v{API_VERSION}",
        "auth": {
            "type": "bearer",
            "header": "Authorization",
            "scheme": "Bearer",
            "token_prefix": agent_keys.PREFIX,
            "example": f"Authorization: Bearer {agent_keys.PREFIX}...",
            "how_to_get_one": (
                "A person issues it from Your account -> Keys for programs. "
                "No key can issue a key, including its own."
            ),
        },
        "manifest": f"{root}/api/agent/v{API_VERSION}/manifest",
        "openapi": f"{root}/api/openapi.json",
        "documentation": f"{root}/llms.txt",
        "rate_limit": {"requests_per_hour": agent_requests.PER_HOUR},
        "start_here": (
            f"GET {root}/api/agent/v{API_VERSION}/manifest with a bearer token. "
            "It replaces a dozen exploratory calls."
        ),
    }


#: Served at `/llms.txt`, which is where a model is now conventionally pointed,
#: and at `/.well-known/llms.txt` for anything that looks there instead.
#:
#: Markdown rather than JSON on purpose: the reader is a language model, the
#: content is prose and examples, and a client that wants structure has the
#: manifest the moment it has a key.
def document(*, base_url: str = "") -> str:
    """The page, with absolute URLs when the caller knows its own origin."""
    root = base_url.rstrip("/")
    agent_base = f"{root}/api/agent/v{API_VERSION}"
    prefix = agent_keys.PREFIX

    conventions = "\n".join(
        f"- **{name.replace('_', ' ')}** — {text}"
        for name, text in CONVENTIONS.items()
        if isinstance(text, str)
    )

    return f"""# Spend Tracker

A self-hosted, multi-currency spend tracker for one household. Version
{__version__}. This file is for programs and language models; a person should
use the app.

Source: {REPOSITORY} (AGPL-3.0-or-later).

## Can I use this without a key?

No. Every useful endpoint needs one, and this page is the only thing served to
a caller without one. Nothing here describes *this* instance — no households,
no accounts, no ids, and no list of routes. It describes the software.

## Getting a key

**A person has to issue it. You cannot issue one for yourself, and neither can
any key.** Ask whoever runs this instance to:

1. open the app and sign in,
2. click their own name, bottom left, for **Your account**,
3. scroll to **Keys for programs** and choose **Give a program a key**,
4. say what it is for, pick the household, and decide whether it may write,
5. confirm with their password and an authenticator code.

The token is shown **once**. It starts `{prefix}`. If it is lost, it cannot be
recovered — only revoked and replaced, because the server keeps a hash of it
and nothing else.

## Using it

Send it as a bearer token on every request:

    Authorization: Bearer {prefix}...

Then fetch the manifest. It is one call, a few hundred tokens, and it replaces
a dozen exploratory ones — the household, what your key may do, the accounts
with their currencies and exponents, the categories, and the endpoints you can
reach:

    curl -H "Authorization: Bearer {prefix}..." \\
         {agent_base}/manifest

The full OpenAPI schema is served at `{root}/api/openapi.json` to any caller
holding a valid key, and to nobody else.

If you would rather have this page as JSON, the same facts are at
`{root}{WELL_KNOWN}`.

## What a key is for

Five jobs, each a few calls and none of them needing the whole register. The
manifest names the endpoint for every step.

1. **Receipts**: store the images as they are, with what you read off them;
   ask which transactions each could belong to; attach it. The app does no
   OCR — reading the receipt is your part, storing and compressing it is the
   app's.
2. **Categorisation**: ask for the categorisation review — each payee's usual
   category, the rows that disagree with it — judge, then categorise in one act.
3. **Work expenses**: find the rows a corporate expense claim describes by
   amount, date and merchant, and flag them as money an employer will pay back.
4. **The combined position**: balances, summaries and reports per currency,
   accounts with their country, and the exchange rates the household actually
   got. Converting is yours, and you say which rate you used.
5. **Transfers**: see what the transfer matcher suggested and what it left
   alone, link the pairs it missed — including between two currencies — and
   rule out wrong suggestions. A person confirms your links.

## Conventions you should read before writing anything

{conventions}

## Limits

- **{agent_requests.PER_HOUR} requests per hour**, per key. Over it you get
  `429` and a `Retry-After` in seconds; wait that long rather than retrying at
  once. The ceiling is high enough for bulk work and exists to catch a loop.
- Keys expire. Ninety days by default, a year at most, and the expiry does not
  slide when you use it.

## What no key can ever do

Not a matter of which scope you were given — no key can do these at all:

- delete anything,
- undo anything,
- create or revoke a key, including its own,
- touch passwords, authenticators, devices or who is in a household,
- reach the database browser.

Everything a program gets wrong has to be undoable by a person, which means a
person stays the only one who can undo.

## Being a good citizen here

- Ask for a **summary** rather than the register. The register can be tens of
  thousands of rows; a summary is a few hundred tokens and the arithmetic is
  already exact.
- Use `since_seq` to read only what changed since last time.
- Money is integer minor units. Do not do arithmetic on it in floating point,
  and do not add two currencies — this ledger has no exchange rate anywhere in
  it, and no endpoint here will give you a total across currencies.
"""
