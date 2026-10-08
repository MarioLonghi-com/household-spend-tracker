"""The app's side of the self-updater's file contract (design notes, Part 5).

The updater runs in its own container and has no listener. The two talk
through the shared `update` volume: this app writes **one request at a time**
as `request.json`, and reads back what the updater writes -- its heartbeat
(`updater.json`), the operation in progress (`status.json`), prepare reports
(`prepared/<id>.json`) and one record per finished request
(`history/<id>.json`).

**This module does not import `updater/`.** The app image does not contain it
(the Dockerfile copies `app/`, `statements/`, `scripts/` and `migrations/`),
so an import would work in a checkout and break in production. The shapes are
written out here instead, and `tests/test_update_endpoints.py` holds the two
sides together: every request this module writes is taken by the updater's
own `intake`, field for field, and every file the updater writes is read back
here.

**Strict out, lenient in** -- the mirror of the updater's rule. A request
carries exactly the keys `updater.contract.FIELDS` names for its kind, at
protocol 1. What the updater writes is read leniently: unknown keys are
ignored, and a state this app does not know reads as `running` (C4).

**The app checks what it can before writing** (5.4), so the owner gets a
sentence at once rather than a refusal two seconds later. The updater's checks
are the ones that count: the app container is the less trusted of the two
(6.1), and nothing here is a substitute for them.

**Writes are atomic and group-shared** (C11): a temporary file in the same
directory, mode 0660 whatever the umask, group 65532 where this process may set
it, `fsync`ed, then *linked* into place -- a link, not a rename, so a request
the updater has not yet taken is never overwritten -- and the directory
`fsync`ed. Nothing is opened through a symlink.

**The recovery code** (3.4, 11.2) is generated here, shown once, and never
written anywhere: not to the volume, not to the log, not to the database. Only
its scrypt hash travels, inside the apply request, in exactly the format the
updater accepts (Part 2f, R10). Between the confirmation being drawn and the
owner pressing *Update*, the hash is held in this process's memory for ten
minutes, bound to the report and to the owner who asked.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__, config

log = logging.getLogger("spendtracker")

#: The protocol this app's requests are written in. Also the app image's
#: `…updater-protocol` label (C4).
PROTOCOL = 1

#: The group every file in the volume belongs to (C11), and the mode of the
#: request this app writes. Group-readable, not group-writable: the updater
#: takes a request by renaming it, which needs the directory's write bit,
#: not the file's, and then only reads it. The volume's directories stay
#: 2770 (C11); nothing else in it is written by the app.
UPDATE_GID = 65532
FILE_MODE = 0o640

#: A heartbeat older than this means no updater (3.1). It is rewritten every
#: 30 seconds.
HEARTBEAT_FRESH_SECONDS = 120

#: The most of any updater file this app reads. A history record holds forty
#: log lines; a report a few dozen migrations.
MAX_READ_BYTES = 1 << 20

#: `X.Y.Z`, ASCII digits only, no prerelease suffix (A5). The same pattern as
#: `updater.contract.VERSION`.
VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

#: States after which nothing is in flight. Anything else -- `accepted`,
#: `running`, `needs_recovery`, and any state this app does not know, which
#: reads as `running` -- is in flight.
SETTLED = frozenset(
    {"succeeded", "rolled_back", "not_started", "refused", "recovered", "left_for_operator"}
)
#: Every state protocol 1 defines (`updater.contract.STATES`).
STATES = SETTLED | {"accepted", "running", "needs_recovery"}
UNKNOWN_STATE_READS_AS = "running"

#: The keys of each request kind at protocol 1 -- exactly these. Mirrors
#: `updater.contract.FIELDS`; the cross-contract test compares the two.
_COMMON = ("protocol", "id", "kind", "created_at", "requested_by")
FIELDS: dict[str, tuple[str, ...]] = {
    "prepare": (*_COMMON, "from_version", "to_version"),
    "apply": (
        *_COMMON,
        "from_version",
        "to_version",
        "prepared_id",
        "digest",
        "updater_digest",
        "accepted_lossy",
        "recovery_hash",
    ),
    "discard": (*_COMMON, "prepared_id"),
    "update_updater": (*_COMMON, "to_version"),
}


def numbers(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def is_version(value: object) -> bool:
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


def is_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: object) -> float | None:
    """A `…Z` UTC time as the updater writes it, or None."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def root() -> Path:
    """The volume, as the live settings name it. Read at call time."""
    return config.settings.update_dir


def in_a_container() -> bool:
    """Whether this process runs inside the app's image.

    The same test as `scripts.in_a_container`, written out because `app/`
    does not import `scripts/`: `SPENDTRACKER_IN_CONTAINER` decides when set,
    and otherwise Docker's `/.dockerenv` marker.
    """
    said = (os.environ.get("SPENDTRACKER_IN_CONTAINER") or "").strip().lower()
    if said in {"1", "true", "yes", "on"}:
        return True
    if said in {"0", "false", "no", "off"}:
        return False
    return Path("/.dockerenv").exists()


# --------------------------------------------------------------------------- #
# Reading what the updater writes
# --------------------------------------------------------------------------- #


def read_json(path: Path) -> dict | None:
    """One file the updater wrote, or None if it is absent or not a document.

    Never through a symlink, only a regular file, never more than
    `MAX_READ_BYTES`. A half-written file cannot be seen -- the updater
    renames into place -- so a parse failure is a file that is not one of
    its, and reads as absent.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_READ_BYTES:
            return None
        data = os.read(fd, MAX_READ_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_READ_BYTES:
        return None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _documents(directory: Path) -> list[tuple[str, dict]]:
    """Every `<uuid4>.json` in one of the volume's directories, as (id, doc)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    found = []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if not dot or ext != "json" or not is_uuid4(stem):
            continue
        doc = read_json(directory / name)
        if doc is not None:
            found.append((stem, doc))
    return found


def _str(doc: dict, key: str) -> str | None:
    value = doc.get(key)
    return value if isinstance(value, str) else None


def _bool(doc: dict, key: str) -> bool | None:
    value = doc.get(key)
    return value if isinstance(value, bool) else None


def _number(doc: dict, key: str) -> float | None:
    value = doc.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _strings(doc: dict, key: str) -> list[str]:
    value = doc.get(key)
    return [one for one in value if isinstance(one, str)] if isinstance(value, list) else []


@dataclass(frozen=True)
class Heartbeat:
    """`updater.json`, the keys protocol 1 defines; anything else is ignored."""

    seen_at: str | None
    fresh: bool
    updater_version: str | None
    image_digest: str | None
    engine: str | None
    engine_version: str | None
    rootless: bool | None
    layout: str | None
    socket: str | None
    hook: bool | None
    busy: bool | None
    role: str | None
    protocols: str | None
    api_version: str | None
    engine_api: str | None
    container: str | None
    #: The refusal's one sentence when `socket` is one (R24): what the
    #: Updates section says in the `refused` case.
    socket_sentence: str | None = None


def heartbeat(now: float | None = None) -> Heartbeat | None:
    doc = read_json(root() / "updater.json")
    if doc is None:
        return None
    now = time.time() if now is None else now
    seen = parse_iso(doc.get("seen_at"))
    # A heartbeat dated in the future by more than the request skew is not
    # fresh either: it is a clock nobody should trust.
    fresh = seen is not None and -60 <= now - seen <= HEARTBEAT_FRESH_SECONDS
    return Heartbeat(
        seen_at=_str(doc, "seen_at"),
        fresh=fresh,
        updater_version=_str(doc, "updater_version"),
        image_digest=_str(doc, "image_digest"),
        engine=_str(doc, "engine"),
        engine_version=_str(doc, "engine_version"),
        rootless=_bool(doc, "rootless"),
        layout=_str(doc, "layout"),
        socket=_str(doc, "socket"),
        hook=_bool(doc, "hook"),
        busy=_bool(doc, "busy"),
        role=_str(doc, "role"),
        protocols=_str(doc, "protocols"),
        api_version=_str(doc, "api_version"),
        engine_api=_str(doc, "engine_api"),
        container=_str(doc, "container"),
        socket_sentence=_str(doc, "socket_sentence"),
    )


@dataclass(frozen=True)
class Status:
    """`status.json` read as `updater.contract.read_status` reads it."""

    state: str
    id: str | None
    kind: str | None
    step: str | None
    sentences: list[str]
    updated_at: str | None


def status() -> Status | None:
    doc = read_json(root() / "status.json")
    if doc is None:
        return None
    state = doc.get("state")
    return Status(
        state=state if state in STATES else UNKNOWN_STATE_READS_AS,
        id=_str(doc, "id"),
        kind=_str(doc, "kind"),
        step=_str(doc, "step"),
        sentences=_strings(doc, "sentences"),
        updated_at=_str(doc, "updated_at"),
    )


def in_flight(now: float | None = None) -> bool:
    """Whether a request is pending or running, so another would be refused.

    A `request.json` the updater has not yet taken; a `status.json` whose
    state is not settled; or a heartbeat that says busy.
    """
    if os.path.lexists(root() / "request.json"):
        return True
    current = status()
    if current is not None and current.state not in SETTLED:
        return True
    beat = heartbeat(now)
    return bool(beat and beat.fresh and beat.busy)


@dataclass(frozen=True)
class Migration:
    revision: str
    title: str | None
    reversible: str
    note: str | None


@dataclass(frozen=True)
class Report:
    """`prepared/<id>.json`: what an apply would do, bound to both digests."""

    id: str
    from_version: str
    to_version: str
    digest: str | None
    updater_digest: str | None
    expires_at: str
    database_stamp: str | None
    pending: list[Migration]
    #: The revisions an apply must accept, one box each: every one not
    #: `clean`, `undeclared` included (3.4).
    lossy: list[str]
    sizes: dict
    attestations: dict


def _report(report_id: str, doc: dict) -> Report | None:
    from_version, to_version = doc.get("from_version"), doc.get("to_version")
    expires = _str(doc, "expires_at")
    if not (is_version(from_version) and is_version(to_version) and expires):
        return None
    pending = [
        Migration(
            revision=str(one.get("revision")),
            title=_str(one, "title"),
            reversible=str(one.get("reversible") or "undeclared"),
            note=_str(one, "note"),
        )
        for one in doc.get("pending") or []
        if isinstance(one, dict)
    ]
    return Report(
        id=report_id,
        from_version=str(from_version),
        to_version=str(to_version),
        digest=_str(doc, "digest"),
        updater_digest=_str(doc, "updater_digest"),
        expires_at=expires,
        database_stamp=_str(doc, "database_stamp"),
        pending=pending,
        lossy=sorted({one.revision for one in pending if one.reversible != "clean"}),
        sizes=doc.get("sizes") if isinstance(doc.get("sizes"), dict) else {},
        attestations=doc.get("attestations") if isinstance(doc.get("attestations"), dict) else {},
    )


def report(prepared_id: object, now: float | None = None) -> Report | None:
    """The report called `prepared_id`, if it is current for this version.

    Current means unexpired and prepared *from* the version running now: a
    report goes stale after its 24 hours or as soon as the running version
    changes (3.4).
    """
    if not is_uuid4(prepared_id):
        return None
    doc = read_json(root() / "prepared" / f"{prepared_id}.json")
    if doc is None:
        return None
    found = _report(str(prepared_id), doc)
    now = time.time() if now is None else now
    expires = parse_iso(found.expires_at) if found else None
    if found is None or expires is None or expires <= now or found.from_version != __version__:
        return None
    return found


def newest_report(now: float | None = None) -> Report | None:
    now = time.time() if now is None else now
    current = [
        one
        for report_id, _ in _documents(root() / "prepared")
        if (one := report(report_id, now)) is not None
    ]
    return max(current, key=lambda one: one.expires_at, default=None)


@dataclass(frozen=True)
class Outcome:
    """One `history/<id>.json`, as the screen shows it (3.8)."""

    id: str
    kind: str | None
    state: str
    sentence: str | None
    code: str | None
    finished_at: str | None
    started_at: str | None
    failed_step: str | None
    backup: str | None
    duration_s: float | None
    gap_s: float | None
    log_tail: list[str]


def _outcome(record_id: str, doc: dict) -> Outcome:
    state = doc.get("state")
    return Outcome(
        id=record_id,
        kind=_str(doc, "kind"),
        state=state if state in STATES else UNKNOWN_STATE_READS_AS,
        sentence=_str(doc, "sentence"),
        code=_str(doc, "code"),
        finished_at=_str(doc, "finished_at"),
        started_at=_str(doc, "started_at"),
        failed_step=_str(doc, "failed_step"),
        backup=_str(doc, "backup"),
        duration_s=_number(doc, "duration_s"),
        gap_s=_number(doc, "gap_s"),
        log_tail=_strings(doc, "log_tail")[-40:],
    )


def history() -> list[Outcome]:
    """Every record but the updater's answers to `ping`, newest first."""
    found = [
        _outcome(record_id, doc)
        for record_id, doc in _documents(root() / "history")
        if doc.get("kind") != "ping"
    ]
    return sorted(found, key=lambda one: one.finished_at or "", reverse=True)


# --------------------------------------------------------------------------- #
# Dismissed outcomes
# --------------------------------------------------------------------------- #
#
# An outcome stays at the top of the Updates section until the owner dismisses
# it (3.8). The history records are the updater's, so the dismissal is kept on
# this side: one small file in the data directory, outside the ledger -- the
# record of an update has to survive the ledger being rolled back (10.1, A9),
# and so does "I have read it".


def _seen_path() -> Path:
    return config.settings.data_dir / "update-outcomes-seen.json"


def dismissed() -> set[str]:
    doc = read_json(_seen_path())
    return set(_strings(doc, "seen")) if doc else set()


def newest_outcome() -> Outcome | None:
    gone = dismissed()
    return next((one for one in history() if one.id not in gone), None)


def dismiss(record_id: str) -> bool:
    """Mark one outcome seen. False when there is no such record."""
    if not is_uuid4(record_id) or not any(one.id == record_id for one in history()):
        return False
    known = {one.id for one in history()}
    # Only ids that still have a record are kept, so the file stays small.
    seen = sorted((dismissed() & known) | {record_id})
    _write_private(_seen_path(), (json.dumps({"seen": seen}, indent=2) + "\n").encode())
    return True


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Update backups
# --------------------------------------------------------------------------- #


def update_backup_names() -> set[str]:
    """The backup folders the updater's drills took, by folder name.

    An update backup is a `backups/<stamp>/` folder a history record names
    (8.7). A folder `scripts.backup` wrote by hand has the same shape and is
    not one: it is never pruned and never protected.
    """
    return {Path(one.backup).name for one in history() if one.backup}


# --------------------------------------------------------------------------- #
# The recovery code
# --------------------------------------------------------------------------- #

#: Crockford's base-32 alphabet: no I, L, O or U.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: Seven groups of four symbols: 140 random bits, at least the 128 the design
#: notes ask for, and groups of equal length are easier to copy than 26 symbols
#: split unevenly.
CODE_GROUPS = 7
CODE_GROUP_LENGTH = 4

#: scrypt's parameters: N = 2^15, r = 8, p = 1 -- the updater's floor exactly
#: (R10). One verification costs 128 * r * N = 32 MiB, half the updater's
#: 64 MiB ceiling, so it fits its 128 MiB `mem_limit` with room. More cost
#: buys nothing here: the code has 140 bits of entropy, and the updater limits
#: attempts besides.
SCRYPT_LN = 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_HASH_BYTES = 32
#: hashlib's own default ceiling is 32 MiB, which is exactly what N = 2^15,
#: r = 8 needs and so one byte short once OpenSSL's overhead is counted.
_SCRYPT_MAXMEM = 64 * 1024 * 1024

#: How long a code is held, bound to its report, waiting for *Update*.
CODE_SECONDS = 10 * 60


def new_recovery_code() -> str:
    """`XXXX-XXXX-…`, seven groups of Crockford base-32 from `secrets`."""
    symbols = "".join(secrets.choice(CROCKFORD) for _ in range(CODE_GROUPS * CODE_GROUP_LENGTH))
    return "-".join(
        symbols[i : i + CODE_GROUP_LENGTH] for i in range(0, len(symbols), CODE_GROUP_LENGTH)
    )


def canonical_code(code: str) -> str:
    """The text that is hashed, and that a verifier must hash the same way.

    Crockford's reading rules: case does not matter, hyphens are only for the
    eye, `O` is zero and `I` and `L` are one. So a code typed back in lower
    case, without its hyphens or with an `O` for a `0`, still opens recovery.
    The updater's check (Part 11) applies exactly this before hashing.
    """
    text = code.strip().upper().replace("-", "").replace(" ", "")
    return text.replace("O", "0").replace("I", "1").replace("L", "1")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def hash_recovery_code(code: str, *, salt: bytes | None = None) -> str:
    """`scrypt$ln=<n>,r=<r>,p=<p>$<salt>$<hash>`, unpadded base64 (R10)."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES) if salt is None else salt
    digest = hashlib.scrypt(
        canonical_code(code).encode("ascii"),
        salt=salt,
        n=1 << SCRYPT_LN,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=SCRYPT_HASH_BYTES,
    )
    return f"scrypt$ln={SCRYPT_LN},r={SCRYPT_R},p={SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def recovery_code_matches(code: str, hashed: str) -> bool:
    """Whether `code` is the one `hashed` was made from. The updater's side
    of this is its own; this one is what the tests hold the format to."""
    m = re.fullmatch(r"scrypt\$ln=(\d+),r=(\d+),p=(\d+)\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)", hashed)
    if not m:
        return False
    ln, r, p = (int(m.group(i)) for i in (1, 2, 3))

    def unb64(text: str) -> bytes:
        return base64.b64decode(text + "=" * (-len(text) % 4))

    want = unb64(m.group(5))
    got = hashlib.scrypt(
        canonical_code(code).encode("ascii"),
        salt=unb64(m.group(4)),
        n=1 << ln,
        r=r,
        p=p,
        maxmem=_SCRYPT_MAXMEM,
        dklen=len(want),
    )
    return secrets.compare_digest(got, want)


@dataclass(frozen=True)
class _Held:
    recovery_hash: str
    prepared_id: str
    user_id: str
    expires_at: float


_held: dict[str, _Held] = {}
_held_lock = threading.Lock()


def _prune(now: float) -> None:
    for code_id in [k for k, v in _held.items() if v.expires_at <= now]:
        del _held[code_id]


def issue_recovery_code(prepared_id: str, user_id: str, now: float | None = None) -> tuple[str, str, float]:
    """A fresh code for the confirmation of `prepared_id`: `(code_id, code, expires_at)`.

    Only the hash is kept, in memory, for `CODE_SECONDS`. The code is returned
    to be shown once and is not kept anywhere. Drawing the confirmation again
    issues a new code and forgets this owner's earlier ones, so the code on
    the screen is always the one *Update* will use.
    """
    now = time.time() if now is None else now
    code = new_recovery_code()
    hashed = hash_recovery_code(code)
    code_id = secrets.token_urlsafe(16)
    with _held_lock:
        _prune(now)
        for old in [k for k, v in _held.items() if v.user_id == user_id]:
            del _held[old]
        _held[code_id] = _Held(hashed, prepared_id, user_id, now + CODE_SECONDS)
    return code_id, code, now + CODE_SECONDS


def take_recovery_hash(code_id: object, *, prepared_id: str, user_id: str, now: float | None = None) -> str | None:
    """The hash held under `code_id`, spent -- or None.

    None when there is no such code, it has expired, or it was issued to
    another owner or for another report. The entry goes either way, as a
    step-up grant does: a code that has been presented once is not held for a
    second try.
    """
    now = time.time() if now is None else now
    if not isinstance(code_id, str):
        return None
    with _held_lock:
        _prune(now)
        held = _held.pop(code_id, None)
    if held is None or held.user_id != user_id or held.prepared_id != prepared_id:
        return None
    return held.recovery_hash


def forget_recovery_codes() -> None:
    """Every held hash. For tests, and for nothing else."""
    with _held_lock:
        _held.clear()


# --------------------------------------------------------------------------- #
# Writing a request
# --------------------------------------------------------------------------- #


class RequestPending(Exception):
    """A `request.json` is already there, not yet taken by the updater."""


class NoVolume(Exception):
    """The volume is not mounted here, or is not a directory."""


_write_lock = threading.Lock()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_request(doc: dict) -> Path:
    """Put `doc` in the volume as `request.json`, atomically, never over another.

    Raises `RequestPending` when one is already waiting, and `NoVolume` when
    there is no volume to write into.
    """
    kind = doc.get("kind")
    if kind not in FIELDS or set(doc) != set(FIELDS[kind]) or doc.get("protocol") != PROTOCOL:
        raise ValueError(f"not a protocol {PROTOCOL} {kind} request")  # a bug here, not input
    where = root()
    try:
        st = os.lstat(where)
    except FileNotFoundError:
        raise NoVolume(str(where)) from None
    if not stat.S_ISDIR(st.st_mode):
        raise NoVolume(str(where))
    target = where / "request.json"
    data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")

    with _write_lock:
        tmp = where / f".request.json.{secrets.token_hex(6)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(tmp, flags, FILE_MODE)
        try:
            try:
                os.fchmod(fd, FILE_MODE)  # whatever the umask took away
                with contextlib.suppress(PermissionError):
                    if os.fstat(fd).st_gid != UPDATE_GID:
                        os.fchown(fd, -1, UPDATE_GID)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                # A link fails if the name exists -- a symlink planted there
                # included -- so a request not yet taken is never replaced.
                os.link(tmp, target, follow_symlinks=False)
            except FileExistsError:
                raise RequestPending() from None
            except OSError as problem:
                if problem.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                    raise
                # A filesystem without hard links: the lock above is then the
                # only thing between two writers in this process, and the
                # check-then-rename is as close as it gets.
                if os.path.lexists(target):
                    raise RequestPending() from None
                os.replace(tmp, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        _fsync_dir(where)
    return target


def _base(kind: str, requested_by: str, now: float | None) -> dict:
    return {
        "protocol": PROTOCOL,
        "id": str(uuid.uuid4()),
        "kind": kind,
        "created_at": iso(time.time() if now is None else now),
        "requested_by": requested_by,
    }


def prepare_request(to_version: str, *, requested_by: str, now: float | None = None) -> dict:
    return {
        **_base("prepare", requested_by, now),
        "from_version": __version__,
        "to_version": to_version,
    }


def apply_request(
    found: Report,
    *,
    digest: str,
    updater_digest: str,
    accepted_lossy: list[str],
    recovery_hash: str,
    requested_by: str,
    now: float | None = None,
) -> dict:
    return {
        **_base("apply", requested_by, now),
        "from_version": __version__,
        "to_version": found.to_version,
        "prepared_id": found.id,
        "digest": digest,
        "updater_digest": updater_digest,
        "accepted_lossy": sorted(accepted_lossy),
        "recovery_hash": recovery_hash,
    }


def discard_request(prepared_id: str, *, requested_by: str, now: float | None = None) -> dict:
    return {**_base("discard", requested_by, now), "prepared_id": prepared_id}


def update_updater_request(to_version: str, *, requested_by: str, now: float | None = None) -> dict:
    return {**_base("update_updater", requested_by, now), "to_version": to_version}
