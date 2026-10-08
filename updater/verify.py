"""Proving where an image came from before it is pulled (design notes, Part 7).

A release offers a version, `X.Y.Z`; the registry resolves its tag to a digest
`D`. Before the updater pulls `D` -- the app image or its own -- this module
fetches the build attestation `release.yml` pushed beside the image and checks
that it says *this repository's release workflow, run for tag `vX.Y.Z` on a
GitHub-hosted runner, built exactly `D`*. Anything less is refused, and there
is no switch, variable or argument that skips the check.

**The one module of the updater that imports outside the standard library.**
It uses `sigstore` (the 4.x API, `requirements-updater.txt`) to verify the
signature, the certificate chain and the Rekor inclusion proof; the rest of
`updater/` stays standard library only, and nothing here imports `app`.

The eight rules of 7.2, and where each is enforced:

1. The bundle is the one at the registry's referrers fallback tag
   `sha256-<D hex>` of the same repository (ghcr has no referrers API), read
   with the anonymous pull token; every manifest and blob fetched by digest is
   hashed here and compared (`fetch_bundles`). The statement's predicate type
   is SLSA provenance v1 (`check_statement`).
2. The signature, the Fulcio chain and the Rekor inclusion proof and
   checkpoint are verified by `sigstore` against the trusted root, from the
   bundle alone: no Rekor lookup. The bundle carries no RFC 3161 timestamp, so
   Rekor's `integratedTime` is the time anchor.
3. Issuer: certificate extensions `.1.1` and `.1.8` (`policy_for`).
4. Identity: the SAN exactly, and `.1.18`, both
   `https://github.com/MarioLonghi-com/household-spend-tracker/.github/workflows/release.yml@refs/tags/vX.Y.Z`.
5. Repository id `.1.15` and owner id `.1.17` equal `REPOSITORY_ID` and
   `OWNER_ID`, and the provenance's `internalParameters.github` agrees. A name
   can be renamed and taken; an id cannot.
6. `.1.11` is `github-hosted` and the provenance's `runner_environment` too;
   `.1.20` and the provenance's `event_name` are `push`.
7. A subject of the statement is `D`, named as the repository. For a
   multi-arch release `D` is the index digest (C6): nothing here reads a media
   type, so an index and a single manifest verify alike. The provenance's
   commit -- also bound to the certificate's `.1.13` -- is returned, and
   `labels_agree` compares it, and the version, with the labels of the
   platform image the engine pulled. That comparison is the caller's, after
   the pull (4.4, P5).
8. `X.Y.Z` has no prerelease suffix: `VERSION` admits nothing else.

**Which trust root.** Online first: `sigstore`'s TUF client refreshes the
production root. If that fails -- TUF unreachable, or no writable cache in a
read-only container -- the root committed beside this module
(`trusted_root.json`, the copy the pinned `sigstore` wheel carries, 7.5) is
used instead, and `Verified.root` says which one verified. A refusal under
the online root is final; the embedded root is a fallback for *getting* a
root, never a second chance at verifying.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import certifi
from sigstore.errors import TUFError
from sigstore.models import Bundle, ClientTrustConfig, TrustedRoot
from sigstore.verify import Verifier
from sigstore.verify import policy as P

from updater.contract import label_version

# --------------------------------------------------------------------------- #
# What a genuine release looks like
# --------------------------------------------------------------------------- #

REGISTRY = "https://ghcr.io"
#: The two repositories an attestation may be for, as the engine client names
#: them (`updater.engine.REPOSITORIES`), and their path on the registry.
REPOSITORIES = {
    "ghcr.io/mariolonghi-com/household-spend-tracker": "mariolonghi-com/household-spend-tracker",
    "ghcr.io/mariolonghi-com/household-spend-tracker-updater": "mariolonghi-com/household-spend-tracker-updater",
}

#: Ids, not names (7.2 rule 5). From the certificates of 0.7.0 and 0.7.1.
REPOSITORY_ID = "1404177156"
OWNER_ID = "335945572"

SOURCE = "https://github.com/MarioLonghi-com/household-spend-tracker"
WORKFLOW = f"{SOURCE}/.github/workflows/release.yml"
WORKFLOW_PATH = ".github/workflows/release.yml"
ISSUER = "https://token.actions.githubusercontent.com"
RUNNER = "github-hosted"
TRIGGER = "push"

PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUNDLE_ARTIFACT_TYPE = "application/vnd.dev.sigstore.bundle"

#: `X.Y.Z` and nothing after it (rule 8).
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")

REVISION_LABEL = "org.opencontainers.image.revision"
VERSION_LABEL = "org.opencontainers.image.version"

EMBEDDED_ROOT = Path(__file__).with_name("trusted_root.json")

# --------------------------------------------------------------------------- #
# How much of the registry is read
# --------------------------------------------------------------------------- #

#: Seconds per request.
TIMEOUT = 20
#: Bytes. A bundle is about 11 KB, its manifest under 1 KB.
MAX_TOKEN = 16 * 1024
MAX_MANIFEST = 64 * 1024
MAX_BUNDLE = 512 * 1024
#: Attestations considered at one fallback tag. Today there is one.
MAX_BUNDLES = 4
#: Where ghcr sends a blob request: one redirect, to this host only, over the
#: registry's own scheme, without the token. Manifests are never redirected.
BLOB_HOSTS = ("pkg-containers.githubusercontent.com",)

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
)
#: What a release tag may resolve to (P2): an OCI index or manifest, or the
#: Docker schema 2 equivalents a single-platform push still produces.
RESOLVE_TYPES = (
    *MANIFEST_TYPES,
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


class Refused(Exception):
    """The image's origin could not be proven. `rule` names what failed; `detail` says how."""

    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


@dataclass(frozen=True)
class Verified:
    """What a verified attestation established about `digest`."""

    repository: str
    digest: str
    version: str
    #: The commit the release was built from: the provenance's, which the
    #: certificate's `.1.13` names as well.
    commit: str
    #: `tuf` when the online root verified, `embedded` when the root committed
    #: beside this module did (7.5).
    root: str

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "digest": self.digest,
            "version": self.version,
            "commit": self.commit,
            "root": self.root,
        }


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect surfaces as an `HTTPError`; `_get` decides what to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _opener() -> urllib.request.OpenerDirector:
    # certifi's store, as app/services/platform.py does: the python.org macOS
    # build reads no system store, and the image's is whatever its base ships.
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))


def _read(response, cap: int) -> bytes:  # noqa: ANN001
    length = response.headers.get("Content-Length")
    if length is not None and length.isdigit() and int(length) > cap:
        raise Refused("attestation", f"the registry offered {length} bytes where at most {cap} are read")
    body = response.read(cap + 1)
    if len(body) > cap:
        raise Refused("attestation", f"the registry sent more than {cap} bytes")
    return body


class Registry:
    """Anonymous, read-only access to one registry: token, manifests and blobs.

    `base` and `blob_hosts` exist for the tests' fake registry; the updater
    uses the defaults.
    """

    def __init__(self, base: str = REGISTRY, blob_hosts: tuple[str, ...] = BLOB_HOSTS,
                 timeout: float = TIMEOUT) -> None:
        self.base = base.rstrip("/")
        self.scheme = urllib.parse.urlsplit(self.base).scheme
        self.blob_hosts = blob_hosts
        self.timeout = timeout
        self._opener = _opener()

    def _get(self, url: str, *, token: str | None, accept: str | None, cap: int,
             follow_to_blob_host: bool = False) -> bytes:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if accept:
            headers["Accept"] = accept
        request = urllib.request.Request(url, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return _read(response, cap)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and follow_to_blob_host:
                location = e.headers.get("Location", "")
                target = urllib.parse.urlsplit(urllib.parse.urljoin(url, location))
                if target.scheme != self.scheme or target.hostname not in self.blob_hosts:
                    raise Refused(
                        "attestation", f"the registry redirected to {target.scheme}://{target.hostname}"
                    ) from e
                # Off the registry: the token stays behind, and no second redirect.
                plain = urllib.request.Request(target.geturl())
                try:
                    with self._opener.open(plain, timeout=self.timeout) as response:
                        return _read(response, cap)
                except urllib.error.HTTPError as e2:
                    raise Refused("attestation", f"the blob host answered {e2.code}") from e2
                except (urllib.error.URLError, OSError) as e2:
                    raise Refused("unreachable", f"the blob host could not be reached ({type(e2).__name__})") from e2
            if e.code == 404:
                raise Refused("attestation", "the registry has no attestation for this image") from e
            raise Refused("attestation", f"the registry answered {e.code}") from e
        except Refused:
            raise
        except (urllib.error.URLError, OSError) as e:
            raise Refused("unreachable", f"the registry could not be reached ({type(e).__name__})") from e

    def token(self, path: str) -> str:
        scope = urllib.parse.quote(f"repository:{path}:pull", safe=":/")
        body = self._get(f"{self.base}/token?scope={scope}", token=None, accept=None, cap=MAX_TOKEN)
        doc = _json(body, "the registry's token")
        token = doc.get("token") if isinstance(doc, dict) else None
        if not isinstance(token, str) or not token:
            raise Refused("attestation", "the registry gave no pull token")
        return token

    def manifest(self, path: str, reference: str, token: str,
                 types: tuple[str, ...] = MANIFEST_TYPES) -> bytes:
        return self._get(f"{self.base}/v2/{path}/manifests/{reference}", token=token,
                         accept=", ".join(types), cap=MAX_MANIFEST)

    def blob(self, path: str, digest: str, token: str) -> bytes:
        return self._get(f"{self.base}/v2/{path}/blobs/{digest}", token=token, accept=None,
                         cap=MAX_BUNDLE, follow_to_blob_host=True)


def _json(body: bytes, what: str) -> object:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        raise Refused("attestation", f"{what} is not JSON") from e


def _by_digest(body: bytes, digest: str, what: str) -> bytes:
    """`body`, if it hashes to `digest`: nothing the registry says about a digest is taken on trust."""
    got = "sha256:" + hashlib.sha256(body).hexdigest()
    if got != digest:
        raise Refused("attestation", f"{what} hashes to {got}, not {digest}")
    return body


def fetch_bundles(repository: str, digest: str, registry: Registry | None = None) -> list[bytes]:
    """The attestation bundles stored for `digest` in `repository`, as the registry holds them (rule 1).

    Reads the fallback tag `sha256-<hex>` (an OCI index), and for each entry
    whose artifact type is a Sigstore bundle, its manifest by digest -- which
    must name `digest` as its subject -- and the manifest's one layer by
    digest. Raises `Refused` when there is none.
    """
    path = _repository_path(repository)
    _check_digest(digest)
    registry = registry or Registry()
    token = registry.token(path)
    index = _json(registry.manifest(path, "sha256-" + digest.removeprefix("sha256:"), token), "the attestation index")
    entries = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise Refused("attestation", "the attestation index lists no manifests")
    wanted = [m for m in entries if isinstance(m, dict)
              and str(m.get("artifactType", "")).startswith(BUNDLE_ARTIFACT_TYPE)
              and isinstance(m.get("digest"), str) and DIGEST.fullmatch(m["digest"])]
    if not wanted:
        raise Refused("attestation", "the registry has no attestation bundle for this image")
    bundles = []
    for entry in wanted[:MAX_BUNDLES]:
        manifest = _json(_by_digest(registry.manifest(path, entry["digest"], token), entry["digest"],
                                    "the bundle's manifest"), "the bundle's manifest")
        if not isinstance(manifest, dict):
            raise Refused("attestation", "the bundle's manifest is not an object")
        subject = manifest.get("subject")
        if not isinstance(subject, dict) or subject.get("digest") != digest:
            raise Refused("attestation", "the bundle's manifest is about another image")
        layers = manifest.get("layers")
        if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], dict):
            raise Refused("attestation", "the bundle's manifest does not hold exactly one layer")
        layer = layers[0].get("digest")
        if not isinstance(layer, str) or not DIGEST.fullmatch(layer):
            raise Refused("attestation", "the bundle's layer has no digest")
        bundles.append(_by_digest(registry.blob(path, layer, token), layer, "the bundle"))
    return bundles


# --------------------------------------------------------------------------- #
# Resolving a release to a digest, without pulling (4.4 P2, P3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Resolved:
    """What a release tag names: the digest to verify and pull, and how big it is."""

    repository: str
    version: str
    #: The index digest for a multi-arch release (C6), else the manifest's.
    digest: str
    #: Compressed bytes of the platform image the engine would pull: its
    #: layers and config, from the manifest. A registry says nothing of the
    #: unpacked size; `updater.prepare` estimates that from this.
    size: int
    platform: str


def resolve(
    repository: str, version: str, arch: str = "amd64", registry: Registry | None = None
) -> Resolved:
    """The digest the tag `version` names in `repository`, and its size, without pulling.

    The digest is the hash of the manifest the registry returned, computed
    here; for an index, the `linux/<arch>` entry's manifest is read by digest
    and hashed too. Raises `Refused` (`unreachable` when the registry cannot
    be reached).
    """
    path = _repository_path(repository)
    _check_version(version)
    registry = registry or Registry()
    token = registry.token(path)
    try:
        body = registry.manifest(path, version, token, RESOLVE_TYPES)
    except Refused as e:
        if e.detail == "the registry has no attestation for this image":  # its 404, on a tag
            raise Refused("release", f"the registry has no {version} of this image") from None
        raise
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    doc = _json(body, "the release's manifest")
    if not isinstance(doc, dict):
        raise Refused("manifest", "the release's manifest is not an object")
    platform = "single"
    if isinstance(doc.get("manifests"), list):
        entries = [
            m
            for m in doc["manifests"]
            if isinstance(m, dict)
            and isinstance(m.get("platform"), dict)
            and m["platform"].get("os") == "linux"
            and m["platform"].get("architecture") == arch
            and isinstance(m.get("digest"), str)
            and DIGEST.fullmatch(m["digest"])
        ]
        if not entries:
            raise Refused("platform", f"the release has no linux/{arch} image")
        inner = entries[0]["digest"]
        doc = _json(
            _by_digest(
                registry.manifest(path, inner, token, RESOLVE_TYPES), inner, "the platform manifest"
            ),
            "the platform manifest",
        )
        platform = f"linux/{arch}"
        if not isinstance(doc, dict):
            raise Refused("manifest", "the platform manifest is not an object")
    size = 0
    for part in [doc.get("config"), *(doc.get("layers") or [])]:
        if isinstance(part, dict) and isinstance(part.get("size"), int):
            size += part["size"]
    return Resolved(repository=repository, version=version, digest=digest, size=size, platform=platform)


# --------------------------------------------------------------------------- #
# The policy
# --------------------------------------------------------------------------- #


def _repository_path(repository: str) -> str:
    try:
        return REPOSITORIES[repository]
    except KeyError:
        raise Refused("repository", f"{repository!r} is not one of this project's images") from None


def _check_digest(digest: str) -> None:
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise Refused("digest", f"{digest!r} is not a sha256 digest")


def _check_version(version: str) -> None:
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise Refused("version", f"{version!r} is not a release version X.Y.Z without a suffix")


def identity(version: str) -> str:
    """The certificate identity a release of `version` carries (rule 4)."""
    _check_version(version)
    return f"{WORKFLOW}@refs/tags/v{version}"


def policy_for(version: str) -> P.AllOf:
    """Rules 3 to 6, as checks on the signing certificate."""
    ident = identity(version)
    return P.AllOf([
        P.OIDCIssuer(ISSUER),                               # rule 3, .1.1
        P.OIDCIssuerV2(ISSUER),                             # rule 3, .1.8
        P.Identity(identity=ident, issuer=ISSUER),          # rule 4, the SAN
        P.OIDCBuildConfigURI(ident),                        # rule 4, .1.18
        P.OIDCSourceRepositoryRef(f"refs/tags/v{version}"),  # rule 4, .1.14
        P.OIDCSourceRepositoryIdentifier(REPOSITORY_ID),    # rule 5, .1.15
        P.OIDCSourceRepositoryOwnerIdentifier(OWNER_ID),    # rule 5, .1.17
        P.OIDCRunnerEnvironment(RUNNER),                    # rule 6, .1.11
        P.OIDCBuildTrigger(TRIGGER),                        # rule 6, .1.20
    ])


def _get(doc: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def check_statement(payload_type: str, payload: bytes, repository: str, digest: str, version: str) -> str:
    """Rules 1, 5, 6 and 7 on the signed statement. Returns the commit it was built from.

    Run only on a payload whose signature has verified; separate so the
    index-digest case and malformed statements can be tested without one.
    """
    _repository_path(repository)
    _check_digest(digest)
    _check_version(version)
    if payload_type != PAYLOAD_TYPE:
        raise Refused("statement", f"the payload is {payload_type!r}, not an in-toto statement")
    st = _json(payload, "the statement")
    if _get(st, "_type") != STATEMENT_TYPE:
        raise Refused("statement", "the payload is not an in-toto v1 statement")
    if _get(st, "predicateType") != PREDICATE_TYPE:
        raise Refused("predicate", f"the predicate is {_get(st, 'predicateType')!r}, not SLSA provenance v1")

    subjects = _get(st, "subject")
    hexdigest = digest.removeprefix("sha256:")
    named = [s for s in subjects if isinstance(s, dict) and _get(s, "digest", "sha256") == hexdigest] \
        if isinstance(subjects, list) else []
    if not named:
        raise Refused("subject", f"the attestation is not about {digest}")
    if not any(s.get("name") == repository for s in named):
        raise Refused("subject", f"the attestation names {digest} as another repository's image")

    gh = _get(st, "predicate", "buildDefinition", "internalParameters", "github")
    expected = {
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": OWNER_ID,
        "runner_environment": RUNNER,
        "event_name": TRIGGER,
    }
    for key, want in expected.items():
        if _get(gh, key) != want:
            raise Refused("provenance", f"the provenance's {key} is {_get(gh, key)!r}, not {want!r}")

    workflow = _get(st, "predicate", "buildDefinition", "externalParameters", "workflow")
    tag = f"refs/tags/v{version}"
    if (_get(workflow, "ref"), _get(workflow, "repository"), _get(workflow, "path")) != (tag, SOURCE, WORKFLOW_PATH):
        raise Refused("provenance", f"the provenance's workflow is not {WORKFLOW_PATH} at {tag}")
    if _get(st, "predicate", "runDetails", "builder", "id") != identity(version):
        raise Refused("provenance", "the provenance's builder is not this release's workflow")

    deps = _get(st, "predicate", "buildDefinition", "resolvedDependencies")
    source = f"git+{SOURCE}@{tag}"
    commits = [_get(d, "digest", "gitCommit") for d in deps if isinstance(d, dict) and d.get("uri") == source] \
        if isinstance(deps, list) else []
    if len(commits) != 1 or not isinstance(commits[0], str) or not COMMIT.fullmatch(commits[0]):
        raise Refused("provenance", f"the provenance does not name one commit for {tag}")
    return commits[0]


def verify_bundle(bundle: bytes, repository: str, digest: str, version: str, root: TrustedRoot) -> str:
    """All eight rules on one bundle, against one trusted root. Returns the commit."""
    pol = policy_for(version)
    try:
        parsed = Bundle.from_json(bundle)
        payload_type, payload = Verifier(trusted_root=root).verify_dsse(parsed, pol)
        commit = check_statement(payload_type, payload, repository, digest, version)
        # The commit the labels are compared with is the certificate's too (.1.13).
        P.OIDCSourceRepositoryDigest(commit).verify(parsed.signing_certificate)
    except Refused:
        raise
    except Exception as e:  # noqa: BLE001 -- V5: whatever the library raises is a refusal
        raise Refused("signature", f"{type(e).__name__}: {e}") from e
    return commit


# --------------------------------------------------------------------------- #
# Trust roots, and the whole check
# --------------------------------------------------------------------------- #


def embedded_root() -> TrustedRoot:
    """The root committed beside this module: the pinned `sigstore` wheel's copy (7.5)."""
    return TrustedRoot.from_file(str(EMBEDDED_ROOT))


def online_root() -> TrustedRoot:
    """Sigstore's production root, refreshed through TUF now. Raises when TUF cannot be reached."""
    return ClientTrustConfig.production(offline=False).trusted_root


def verify(
    repository: str,
    digest: str,
    version: str,
    *,
    fetch: Callable[[str, str], list[bytes]] | None = None,
    tuf_root: Callable[[], TrustedRoot] | None = None,
) -> Verified:
    """Prove `digest` is `repository`'s release `version`, or raise `Refused` (rules 1 to 8).

    Every failure, of whatever kind, is a `Refused`: the caller pulls only on
    a returned `Verified`. `fetch` and `tuf_root` replace the registry and
    the TUF refresh (the tests' offline runs); neither can skip a rule.
    """
    fetch = fetch or fetch_bundles
    tuf_root = tuf_root or online_root
    try:
        _repository_path(repository)
        _check_digest(digest)
        _check_version(version)
        bundles = fetch(repository, digest)
        try:
            root, which = tuf_root(), "tuf"
        except (TUFError, OSError):
            # TUF unreachable, or nowhere to cache it: the embedded root (7.5).
            root, which = embedded_root(), "embedded"
        reasons = []
        for bundle in bundles:
            try:
                commit = verify_bundle(bundle, repository, digest, version, root)
            except Refused as e:
                reasons.append(e)
                continue
            return Verified(repository=repository, digest=digest, version=version, commit=commit, root=which)
        if not reasons:
            raise Refused("attestation", "the registry has no attestation bundle for this image")
        raise reasons[0]
    except Refused:
        raise
    except Exception as e:  # noqa: BLE001 -- V5: any exception means refused
        raise Refused("error", f"{type(e).__name__}: {e}") from e


def labels_agree(verified: Verified, labels: Mapping[str, object] | None) -> None:
    """The pulled platform image is the release that was verified (4.4 P5, 7.2 rule 7), or `Refused`."""
    labels = labels or {}
    if labels.get(REVISION_LABEL) != verified.commit:
        raise Refused("labels", f"the image's revision label is {labels.get(REVISION_LABEL)!r}, "
                                f"not the attested commit {verified.commit}")
    version = labels.get(VERSION_LABEL)
    # One leading `v` is stripped from labels written before C8 (contract.label_version).
    if label_version(version if isinstance(version, str) else None) != verified.version:
        raise Refused("labels", f"the image's version label is {labels.get(VERSION_LABEL)!r}, "
                                f"not {verified.version}")
