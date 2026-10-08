"""The file contract between the app and the updater (design notes, Part 5).

The app writes one request at a time into the shared volume; the updater
validates it here and writes back a heartbeat, a status, prepare reports and
one history record per request. Everything in this module is data and pure
checks: no engine call, no file I/O. `intake` does the taking and the writing.

**Strict in, lenient out.** A request with a key the updater does not know is
refused, and so is one missing a key: no field may name an image, a registry,
a path, a command or an environment variable, and the only way to keep it so
is to accept nothing unlisted. What the updater *writes* is the other way
round: files only ever gain keys within protocol 1, and a reader ignores the
keys it does not know and shows a state it does not know as `running` (C4).

**Protocol windows (C4).** An app image's `updater-protocol` label is the
protocol its requests are written in; an updater accepts every request of
every protocol in its own window, `PROTOCOLS`. `ping` and `update_updater` are
frozen at protocol 1 for ever, so an app newer than its updater can always ask
that updater to replace itself (6.6).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

#: The protocols this updater accepts, inclusive. Widened only by a release
#: whose updater reads every request kind of the new protocol.
PROTOCOLS: tuple[int, int] = (1, 1)
#: The protocol `ping`, `update_updater`, the journal and the handover files
#: are frozen at, and the one every file the updater writes carries.
FROZEN_PROTOCOL = 1

REQUEST_KINDS = ("prepare", "apply", "discard", "update_updater", "ping")
FROZEN_KINDS = ("ping", "update_updater")
#: Part 11's actions, sent by the recovery page as `recovery/request.json`.
RECOVERY_KINDS = (
    "open",
    "retry_rollback",
    "restore_backup",
    "start_matching",
    "download_backup",
    "download_diagnostics",
    "leave_for_operator",
)

#: Every state `status.json` and `history/<id>.json` can carry (5.3), plus the
#: one Part 11's *Stop and leave it to me* marks a record with.
STATES = (
    "accepted",
    "running",
    "succeeded",
    "rolled_back",
    "not_started",
    "refused",
    "needs_recovery",
    "recovered",
    "left_for_operator",
)
#: What a reader shows for a state it does not know (C4).
UNKNOWN_STATE_READS_AS = "running"

ENGINES = ("docker-engine", "docker-desktop", "podman", "podman-machine")
#: What the heartbeat says before an engine has been identified: the socket
#: does not answer, or answers as no engine the updater knows (8.3).
UNKNOWN_ENGINE = "unknown"
#: S2: whether `podman-restart.service` is known to be on. Off Podman it does
#: not apply; on Podman the updater can only infer `enabled`, never `disabled`.
PODMAN_RESTART = ("enabled", "unknown", "not_applicable")
LAYOUTS = ("sidecar", "loopback")
ROLES = ("current", "successor", "standby")
#: `ok`, `outdated` (C3), or one of 8.3's refusal reasons.
SOCKET_STATES = (
    "ok",
    "outdated",
    "permission_denied",
    "eci_blocked",
    "windows_containers",
    "tcp_socket",
    "too_old",
    "unknown_engine",
    "unreachable",
)

#: A request older than this, or dated further ahead than the skew, is stale.
#: Wall-clock time, gaps included: 8.6 makes expiry the exception to "deadlines
#: exclude sleep".
REQUEST_MAX_AGE_SECONDS = 10 * 60
REQUEST_MAX_SKEW_SECONDS = 60

#: `X.Y.Z`, ASCII digits only -- `\d` would admit other scripts' digits -- and
#: no prerelease suffix (A5).
VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{12}")
CREATED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
#: A backup folder's name, as `scripts.backup` stamps it.
BACKUP_STAMP = re.compile(r"[0-9]{8}-[0-9]{6}")
#: The recovery code as the page forwards it. Verified against the hash by the
#: updater, never trusted for its shape alone; this only bounds what is read.
RECOVERY_CODE = re.compile(r"[A-Za-z0-9-]{16,64}")
REQUESTED_BY_MAX = 128

#: The recovery code's hash: `scrypt$ln=<log2 N>,r=<r>,p=<p>$<salt>$<hash>`,
#: salt and hash in unpadded standard base64, the hash 32 bytes. The floor is
#: N = 2^15, r = 8, p = 1. The ceiling keeps one verification inside the
#: updater's 128 MiB (`mem_limit`): scrypt needs 128 * r * N bytes.
RECOVERY_HASH = re.compile(
    r"scrypt\$ln=([0-9]{1,2}),r=([0-9]{1,2}),p=([0-9]{1,2})\$([A-Za-z0-9+/]{22,88})\$([A-Za-z0-9+/]{43})"
)
SCRYPT_FLOOR = {"ln": 15, "r": 8, "p": 1}
SCRYPT_MAX_BYTES = 64 * 1024 * 1024

_COMMON = ("protocol", "id", "kind", "created_at", "requested_by")
#: The keys each kind carries at protocol 1 -- exactly these, no more, no fewer.
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
    # Liveness. Written by the app and, during a handover, by the old updater
    # (H5), so it names no user.
    "ping": ("protocol", "id", "kind", "created_at"),
}

_RECOVERY_COMMON = ("protocol", "id", "kind", "created_at", "code")
#: Part 11's actions. `id` is the id of the update that needs recovery.
RECOVERY_FIELDS: dict[str, tuple[str, ...]] = {
    # Unlocking the page: the code checked, nothing done. Without it the page
    # could not show the history to the owner and to nobody else (#163).
    "open": _RECOVERY_COMMON,
    "retry_rollback": _RECOVERY_COMMON,
    "restore_backup": (*_RECOVERY_COMMON, "backup"),
    "start_matching": (*_RECOVERY_COMMON, "revision"),
    "download_backup": (*_RECOVERY_COMMON, "backup", "include_key"),
    "download_diagnostics": _RECOVERY_COMMON,
    "leave_for_operator": _RECOVERY_COMMON,
}


class Refusal(Exception):
    """A request the updater will not act on. `sentence` is what the owner reads."""

    def __init__(self, code: str, sentence: str) -> None:
        super().__init__(sentence)
        self.code = code
        self.sentence = sentence


# --------------------------------------------------------------------------- #
# Small values
# --------------------------------------------------------------------------- #


def parse_version(value: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch


def label_version(label: str | None) -> str | None:
    """The version an image label carries, or None if it carries none we accept.

    Older releases labelled `v0.7.1`; one leading `v` is stripped (C8).
    """
    if not label:
        return None
    bare = label[1:] if label.startswith("v") else label
    return bare if VERSION.fullmatch(bare) else None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> float:
    if not isinstance(value, str) or not CREATED_AT.fullmatch(value):
        raise ValueError(value)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(value, fmt).replace(tzinfo=UTC).timestamp()


def is_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def recovery_hash_ok(value: object) -> bool:
    """The fixed format, parameters at or above the floor, cost within the ceiling."""
    if not isinstance(value, str):
        return False
    m = RECOVERY_HASH.fullmatch(value)
    if not m:
        return False
    ln, r, p = (int(m.group(i)) for i in (1, 2, 3))
    if ln < SCRYPT_FLOOR["ln"] or r < SCRYPT_FLOOR["r"] or p < SCRYPT_FLOOR["p"]:
        return False
    return 128 * r * (1 << ln) <= SCRYPT_MAX_BYTES


# --------------------------------------------------------------------------- #
# What the updater knows when a request arrives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunningApp:
    """The app container as the updater last inspected it.

    `published` is whether its image's digest is a ghcr digest of this
    repository. `version` is None when the image carries no usable version
    label -- a local build (A6).
    """

    version: str | None
    revision: str | None
    published: bool
    protocol: int = FROZEN_PROTOCOL

    @property
    def local_build(self) -> bool:
        return not (self.version and self.revision and self.published)


@dataclass(frozen=True)
class Context:
    """A snapshot taken by detection, *before* a request is read.

    Validation answers from this and from the volume alone, which is how a
    refusal makes no engine call (5.4, U1).
    """

    app: RunningApp
    updater_version: str
    socket: str = "ok"
    busy: bool = False
    protocols: tuple[int, int] = PROTOCOLS
    #: Detection's sentence for a refused socket (8.3), quoted in the refusal.
    socket_sentence: str | None = None


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Request:
    protocol: int
    id: str
    kind: str
    created_at: str
    requested_by: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    prepared_id: str | None = None
    digest: str | None = None
    updater_digest: str | None = None
    accepted_lossy: tuple[str, ...] | None = None
    recovery_hash: str | None = None


def check_shape(raw: object, ctx_protocols: tuple[int, int] = PROTOCOLS) -> dict:
    """Protocol, kind and the exact set of keys. Returns the raw dict.

    Everything here can be decided from the request alone.
    """
    if not isinstance(raw, dict):
        raise Refusal("not_json", "The request is not a JSON object.")
    protocol = raw.get("protocol")
    lo, hi = ctx_protocols
    if type(protocol) is not int or not lo <= protocol <= hi:
        raise Refusal(
            "protocol",
            f"The request is written in protocol {protocol!r}; this updater reads protocols {lo} to {hi}.",
        )
    kind = raw.get("kind")
    if kind not in FIELDS:
        raise Refusal("kind", f"The request asks for {kind!r}, which this updater does not know.")
    if kind in FROZEN_KINDS and protocol != FROZEN_PROTOCOL:
        raise Refusal("frozen", f"A {kind} request is always written in protocol {FROZEN_PROTOCOL}.")
    allowed = FIELDS[kind]
    unknown = sorted(k for k in raw if k not in allowed)
    if unknown:
        raise Refusal("keys", f"The request carries keys this updater does not accept: {', '.join(unknown)}.")
    missing = [k for k in allowed if k not in raw]
    if missing:
        raise Refusal("keys", f"The request is missing: {', '.join(missing)}.")
    if not is_uuid4(raw["id"]):
        raise Refusal("id", "The request's id is not a UUID4.")
    return raw


def check_created_at(value: object, now: float) -> None:
    try:
        created = parse_iso(value)  # type: ignore[arg-type]
    except ValueError:
        raise Refusal("created_at", "The request's time is not a UTC time like 2026-10-20T21:11:02Z.") from None
    if now - created > REQUEST_MAX_AGE_SECONDS:
        raise Refusal("stale", "The request is more than 10 minutes old.")
    if created - now > REQUEST_MAX_SKEW_SECONDS:
        raise Refusal("future", "The request is dated more than a minute in the future.")


def _version(raw: dict, key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise Refusal("version", f"{key} {value!r} is not a release version like 1.2.3.")
    return value


def check_report(raw: dict, report: dict | None, now: float) -> None:
    """`prepared_id` names an existing, unexpired report for the same versions."""
    if report is None:
        raise Refusal("no_report", "There is no prepare report with that id. Check for updates again.")
    try:
        expires = parse_iso(report.get("expires_at"))  # type: ignore[arg-type]
    except ValueError:
        raise Refusal("no_report", "The prepare report is unreadable. Check for updates again.") from None
    if now >= expires:
        raise Refusal("report_expired", "The prepare report has expired. Check for updates again.")
    if raw["kind"] == "apply" and (
        report.get("from_version") != raw["from_version"] or report.get("to_version") != raw["to_version"]
    ):
        raise Refusal(
            "report_mismatch",
            f"The prepare report is for {report.get('from_version')} to {report.get('to_version')}, "
            f"not {raw['from_version']} to {raw['to_version']}.",
        )


def lossy_revisions(report: dict) -> set[str]:
    """The report's revisions that are not `clean`. `undeclared` counts as lossy."""
    return {
        str(m.get("revision"))
        for m in report.get("pending") or []
        if isinstance(m, dict) and m.get("reversible") != "clean"
    }


def validate(raw: object, ctx: Context, now: float, report: dict | None = None) -> Request:
    """Every rule of 5.2 and 5.4 that the updater can check without the engine.

    `report` is `prepared/<prepared_id>.json` when the request names one. The
    duplicate-id rule is `intake`'s: it has to be decided before the request
    is renamed into the journal under that id. Returns the request, or raises
    `Refusal` with its one sentence.
    """
    raw = check_shape(raw, ctx.protocols)
    kind = raw["kind"]
    check_created_at(raw["created_at"], now)
    if "requested_by" in raw and (
        not isinstance(raw["requested_by"], str) or not 0 < len(raw["requested_by"]) <= REQUESTED_BY_MAX
    ):
        raise Refusal("requested_by", "The request's requested_by is not a short string.")
    if kind == "ping":
        return _request(raw)

    if ctx.busy:
        raise Refusal("concurrent", "Another update is in progress. This request was not queued; send it again later.")
    if ctx.socket == "outdated" and kind in ("prepare", "apply"):
        raise Refusal(
            "outdated",
            "The updater is too old for this container engine. Update the updater first, then try again.",
        )
    if ctx.socket not in ("ok", "outdated"):
        raise Refusal("socket", ctx.socket_sentence or f"The updater cannot use the container engine ({ctx.socket}).")

    if kind == "discard":
        if not is_uuid4(raw["prepared_id"]):
            raise Refusal("no_report", "The prepared_id is not a report id.")
        check_report(raw, report, now)
        return _request(raw)

    if ctx.app.local_build:
        raise Refusal(
            "local_build",
            "This instance runs a locally built image. Self-update starts only from a published release.",
        )
    running = ctx.app.version
    assert running is not None
    to_version = _version(raw, "to_version")

    if kind == "update_updater":
        # The target updater's protocol window (C2, C4) is read from its image
        # labels, which needs the engine; that check belongs to the
        # update_updater prepare (P2-P5), not here.
        if parse_version(to_version) < parse_version(running):
            raise Refusal(
                "app_newer",
                f"The updater of {to_version} is older than the app, which runs {running}.",
            )
        if parse_version(to_version) <= parse_version(ctx.updater_version):
            raise Refusal(
                "updater_not_newer",
                f"The updater already runs {ctx.updater_version}, which is not older than {to_version}.",
            )
        return _request(raw)

    from_version = _version(raw, "from_version")
    if from_version != running:
        raise Refusal("from_mismatch", f"This instance runs {running}, not {from_version}.")
    if parse_version(to_version) <= parse_version(from_version):
        raise Refusal("not_newer", f"{to_version} is not newer than {from_version}. Self-update never goes back.")
    if kind == "prepare":
        return _request(raw)

    # apply
    if not is_uuid4(raw["prepared_id"]):
        raise Refusal("no_report", "The prepared_id is not a report id.")
    check_report(raw, report, now)
    assert report is not None
    for key in ("digest", "updater_digest"):
        value = raw[key]
        if not isinstance(value, str) or not DIGEST.fullmatch(value) or value != report.get(key):
            raise Refusal("digest", "The image digests are not the ones the prepare report verified.")
    lossy = raw["accepted_lossy"]
    if (
        not isinstance(lossy, list)
        or not all(isinstance(x, str) for x in lossy)
        or len(set(lossy)) != len(lossy)
        or set(lossy) != lossy_revisions(report)
    ):
        raise Refusal(
            "lossy",
            "The accepted lossy migrations are not exactly the ones the prepare report lists.",
        )
    if not recovery_hash_ok(raw["recovery_hash"]):
        raise Refusal(
            "recovery_hash",
            "The recovery code's hash is not in the expected scrypt format, or its cost is out of bounds.",
        )
    return _request(raw)


def _request(raw: dict) -> Request:
    values = dict(raw)
    if isinstance(values.get("accepted_lossy"), list):
        values["accepted_lossy"] = tuple(values["accepted_lossy"])
    return Request(**values)


# --------------------------------------------------------------------------- #
# Recovery requests (Part 11): shapes only. Verifying the code is the
# recovery flow's; here only what a request may look like.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RecoveryRequest:
    protocol: int
    id: str
    kind: str
    created_at: str
    code: str
    backup: str | None = None
    revision: str | None = None
    include_key: bool | None = None


def validate_recovery(raw: object, now: float) -> RecoveryRequest:
    if not isinstance(raw, dict):
        raise Refusal("not_json", "The recovery request is not a JSON object.")
    if type(raw.get("protocol")) is not int or raw["protocol"] != FROZEN_PROTOCOL:
        raise Refusal("protocol", f"A recovery request is always written in protocol {FROZEN_PROTOCOL}.")
    kind = raw.get("kind")
    if kind not in RECOVERY_FIELDS:
        raise Refusal("kind", f"The recovery page asked for {kind!r}, which this updater does not know.")
    allowed = RECOVERY_FIELDS[kind]
    unknown = sorted(k for k in raw if k not in allowed)
    if unknown:
        raise Refusal("keys", f"The recovery request carries keys this updater does not accept: {', '.join(unknown)}.")
    missing = [k for k in allowed if k not in raw]
    if missing:
        raise Refusal("keys", f"The recovery request is missing: {', '.join(missing)}.")
    if not is_uuid4(raw["id"]):
        raise Refusal("id", "The recovery request does not name an update.")
    check_created_at(raw["created_at"], now)
    if not isinstance(raw["code"], str) or not RECOVERY_CODE.fullmatch(raw["code"]):
        raise Refusal("code", "That is not a recovery code.")
    if "backup" in raw and (not isinstance(raw["backup"], str) or not BACKUP_STAMP.fullmatch(raw["backup"])):
        raise Refusal("backup", "That is not one of the update backups.")
    if "revision" in raw and (not isinstance(raw["revision"], str) or not REVISION.fullmatch(raw["revision"])):
        raise Refusal("revision", "That is not a database revision.")
    if "include_key" in raw and type(raw["include_key"]) is not bool:
        raise Refusal("include_key", "include_key is true or false.")
    return RecoveryRequest(**raw)


#: Why the maintenance page is in recovery mode (`recovery/mode.json`): an
#: update needing recovery (Part 11), or an app started on an image older than
#: the ledger, with no code in existence (9.2).
RECOVERY_MODES = ("recovery", "ledger_ahead")
#: What the updater answers a recovery request with (`recovery/answer.json`).
#: `accepted` comes first for an action that then runs; `done` or `failed`
#: when it has finished; `refused` when it never started.
RECOVERY_ANSWERS = ("accepted", "done", "failed", "refused")


def recovery_mode(mode: str, update_id: str | None, at: float) -> dict:
    """`recovery/mode.json`: what the page started with `--recovery` is for."""
    if mode not in RECOVERY_MODES:
        raise ValueError(f"mode {mode!r}")
    return {"protocol": FROZEN_PROTOCOL, "mode": mode, "id": update_id, "at": iso(at)}


def recovery_answer(
    request: RecoveryRequest | dict | None, state: str, sentence: str, at: float, code: str | None = None
) -> dict:
    """`recovery/answer.json`. Echoes the request's `id`, `kind` and `created_at`, never its code."""
    if state not in RECOVERY_ANSWERS:
        raise ValueError(f"state {state!r}")
    if isinstance(request, RecoveryRequest):
        echo = {"id": request.id, "kind": request.kind, "created_at": request.created_at}
    else:
        raw = request if isinstance(request, dict) else {}
        echo = {
            k: raw.get(k) if isinstance(raw.get(k), str) and len(raw.get(k)) <= 64 else None
            for k in ("id", "kind", "created_at")
        }
    return {
        "protocol": FROZEN_PROTOCOL,
        **echo,
        "state": state,
        "sentence": sentence,
        "code": code,
        "at": iso(at),
    }


# --------------------------------------------------------------------------- #
# What the updater writes back (5.3). Every shape carries `protocol: 1` and
# only ever gains keys.
# --------------------------------------------------------------------------- #


def _window(lo: tuple[int, ...] | str, hi: tuple[int, ...] | str) -> str:
    def text(v: tuple[int, ...] | str) -> str:
        return v if isinstance(v, str) else ".".join(str(x) for x in v)

    return f"{text(lo)}-{text(hi)}"


@dataclass(frozen=True)
class Heartbeat:
    """`updater.json`, rewritten every 30 seconds."""

    updater_version: str
    image_digest: str
    seen_at: str
    engine: str
    engine_version: str
    rootless: bool
    layout: str
    socket: str
    hook: bool
    busy: bool
    role: str
    api_version: str
    engine_api: str
    container: str
    protocols: str = _window(str(PROTOCOLS[0]), str(PROTOCOLS[1]))
    #: Gained within protocol 1 (C4): the refusal's sentence when `socket` is
    #: one, and whether podman-restart is known to be on (S2).
    socket_sentence: str | None = None
    podman_restart: str = "not_applicable"
    protocol: int = FROZEN_PROTOCOL

    def __post_init__(self) -> None:
        if self.podman_restart not in PODMAN_RESTART:
            raise ValueError(f"podman_restart {self.podman_restart!r}")
        if self.engine not in (*ENGINES, UNKNOWN_ENGINE):
            raise ValueError(f"engine {self.engine!r}")
        if self.layout not in LAYOUTS:
            raise ValueError(f"layout {self.layout!r}")
        if self.role not in ROLES:
            raise ValueError(f"role {self.role!r}")
        if self.socket not in SOCKET_STATES:
            raise ValueError(f"socket {self.socket!r}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Status:
    """`status.json`: the request in progress, its state and the step sentences."""

    id: str
    kind: str
    state: str
    updated_at: str
    step: str | None = None
    sentences: tuple[str, ...] = ()
    protocol: int = FROZEN_PROTOCOL

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"state {self.state!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sentences"] = list(self.sentences)
        return d


@dataclass(frozen=True)
class History:
    """`history/<id>.json`: one per finished request, refusals included."""

    id: str
    kind: str | None
    state: str
    sentence: str
    finished_at: str
    code: str | None = None
    requested_by: str | None = None
    #: The id the request claimed, when the record is filed under another one
    #: (a malformed or duplicate id; see `intake`).
    request_id: str | None = None
    failed_step: str | None = None
    backup: str | None = None
    started_at: str | None = None
    duration_s: float | None = None
    #: Time spent asleep or with the engine down (8.6).
    gap_s: float = 0.0
    log_tail: tuple[str, ...] = ()
    protocol: int = FROZEN_PROTOCOL

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"state {self.state!r}")
        if len(self.log_tail) > 40:
            object.__setattr__(self, "log_tail", tuple(self.log_tail[-40:]))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["log_tail"] = list(self.log_tail)
        return d


@dataclass(frozen=True)
class PreparedReport:
    """`prepared/<id>.json`, bound to both digests. `id` is the prepare request's."""

    id: str
    from_version: str
    to_version: str
    digest: str
    updater_digest: str
    expires_at: str
    database_stamp: str | None
    #: The pending migrations as `scripts.upgrade --check --json` printed them.
    pending: tuple[dict, ...] = ()
    attestations: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    protocol: int = FROZEN_PROTOCOL

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pending"] = [dict(m) for m in self.pending]
        return d


@dataclass(frozen=True)
class StatusView:
    """`status.json` as the app reads it: unknown keys ignored, unknown state is `running`."""

    state: str
    id: str | None
    kind: str | None
    step: str | None
    sentences: tuple[str, ...]


def read_status(doc: object) -> StatusView | None:
    """How a protocol-1 reader takes a status file a newer updater may have written."""
    if not isinstance(doc, dict):
        return None
    state = doc.get("state")
    if state not in STATES:
        state = UNKNOWN_STATE_READS_AS
    sentences = doc.get("sentences")
    return StatusView(
        state=state,
        id=doc.get("id") if isinstance(doc.get("id"), str) else None,
        kind=doc.get("kind") if isinstance(doc.get("kind"), str) else None,
        step=doc.get("step") if isinstance(doc.get("step"), str) else None,
        sentences=tuple(s for s in sentences if isinstance(s, str)) if isinstance(sentences, list) else (),
    )
