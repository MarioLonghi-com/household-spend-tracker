"""Verifying a release's attestation before its image is pulled (design notes 7.2, 15.2).

Against the bundles three real releases pushed to ghcr -- 0.7.0, 0.7.1 and
0.8.0, recorded byte for byte as the registry holds them -- and offline: no
test here reaches the network. The online trust root is replaced by one that
raises `TUFError`, as an unreachable TUF repository does, so every real
bundle is verified against `updater/trusted_root.json`, the root the updater
embeds.

V3's "test Sigstore instance" is replaced by certificates made here with the
Fulcio extensions set to the wrong value one at a time, run through the same
policy object the real check uses; the real certificates pass that policy,
which is what says the made-up ones test the right thing.
"""

from __future__ import annotations

import ast
import base64
import copy
import datetime as dt
import importlib.resources
import inspect
import json
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sigstore.errors import TUFError, VerificationError
from sigstore.models import Bundle

from updater import verify as V

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "updater" / "attestations"
APP = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPDATER = "ghcr.io/mariolonghi-com/household-spend-tracker-updater"

#: Each release's image digest as ghcr resolves its tag, and the commit its
#: provenance -- and its image's revision label -- names. All three were
#: attested by the single-arch pipeline, so `D` is a platform manifest.
RELEASES = {
    "0.7.0": ("sha256:cf94c787efefa8981d0984c73ebeeb573d5a6720e3f13743ea5eecbc78aaf72b",
              "a94e2d68a016ced48f79e0ead11ad13e5c2d233f"),
    "0.7.1": ("sha256:243ca238dcc002a29689eeafa211f94ac3d55c5b22dce98d2dada1bdb0c3da21",
              "987afefca1d1d372df676e338a3d1c43e9a62552"),
    "0.8.0": ("sha256:f6ee70b6a37e3fc8e5b9f84d72008ba66e6ae7ab8c74db282ef96ccfbca63abc",
              "e4f252002fd6bd2d3c6c3265cbc883a1e660151e"),
}


def bundle(version: str) -> bytes:
    return (FIXTURES / f"{version}.sigstore.json").read_bytes()


def tuf_unreachable():
    raise TUFError("Failed to refresh TUF metadata")


def run(version: str, digest: str | None = None, data: bytes | None = None, repository: str = APP,
        **kw) -> V.Verified:
    """`verify` with the registry replaced by `data` (a recorded bundle) and TUF unreachable."""
    of = version if data is None and version in RELEASES else "0.7.1"
    data = bundle(of) if data is None else data
    digest = digest or RELEASES[of][0]
    kw.setdefault("tuf_root", tuf_unreachable)
    return V.verify(repository, digest, version, fetch=lambda r, d: [data], **kw)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may reach TUF or a registry, whatever a test forgets to pass."""
    monkeypatch.setattr(V, "online_root", tuf_unreachable)

    def no_registry(*a, **k):
        raise AssertionError("a test reached for the registry")
    monkeypatch.setattr(V, "fetch_bundles", no_registry)


# --------------------------------------------------------------------------- #
# V1: the genuine bundles verify, and say which root did
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", sorted(RELEASES))
def test_v1_each_real_release_verifies_offline_with_the_embedded_root(version):
    digest, commit = RELEASES[version]
    got = run(version)
    assert got == V.Verified(repository=APP, digest=digest, version=version, commit=commit, root="embedded")


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
def test_v1_a_root_that_tuf_returned_is_recorded_as_tuf(version):
    got = run(version, tuf_root=V.embedded_root)
    assert (got.root, got.commit) == ("tuf", RELEASES[version][1])


@pytest.mark.parametrize("failure", [TUFError("offline"), PermissionError("read-only cache")])
def test_v1_a_tuf_failure_or_an_unwritable_cache_falls_back_to_the_embedded_root(failure):
    def tuf():
        raise failure
    assert run("0.7.1", tuf_root=tuf).root == "embedded"
    assert run("0.8.0", tuf_root=tuf).root == "embedded"


def test_v1_the_first_bundle_that_verifies_wins_when_the_tag_holds_two():
    digest, commit = RELEASES["0.8.0"]
    got = V.verify(APP, digest, "0.8.0", fetch=lambda r, d: [bundle("0.7.1"), bundle("0.8.0")],
                   tuf_root=tuf_unreachable)
    assert got.commit == commit
    with pytest.raises(V.Refused) as e:
        V.verify(APP, digest, "0.8.0", fetch=lambda r, d: [bundle("0.7.1"), bundle("0.7.0")],
                 tuf_root=tuf_unreachable)
    # The first bundle's reason is the one reported.
    assert e.value.rule == "signature" and "refs/tags/v0.8.0" in e.value.detail


# --------------------------------------------------------------------------- #
# V2: each refused for another release's tag
# --------------------------------------------------------------------------- #

PAIRS = [(a, b) for a in sorted(RELEASES) for b in sorted(RELEASES) if a != b]


@pytest.mark.parametrize(("real", "offered"), PAIRS)
def test_v2_a_bundle_offered_as_another_version_is_refused_on_its_identity(real, offered):
    with pytest.raises(V.Refused) as e:
        run(offered, digest=RELEASES[real][0], data=bundle(real))
    assert e.value.rule == "signature"
    assert f"release.yml@refs/tags/v{offered}" in e.value.detail


@pytest.mark.parametrize(("real", "other"), PAIRS)
def test_v2_a_bundle_offered_for_another_release_digest_is_refused_on_its_subject(real, other):
    with pytest.raises(V.Refused) as e:
        run(real, digest=RELEASES[other][0], data=bundle(real))
    assert e.value.rule == "subject"
    assert RELEASES[other][0] in e.value.detail


@pytest.mark.parametrize("version", ["0.7.1", "0.8.0"])
def test_v2_the_app_attestation_does_not_verify_the_updater_repository(version):
    with pytest.raises(V.Refused) as e:
        run(version, repository=UPDATER)
    assert (e.value.rule, "another repository" in e.value.detail) == ("subject", True)


# --------------------------------------------------------------------------- #
# V3: wrong workflow, repository, ids, runner, trigger, issuer
# --------------------------------------------------------------------------- #

OID = "1.3.6.1.4.1.57264.1."


def _utf8(value: str) -> bytes:
    raw = value.encode()
    assert len(raw) < 128
    return bytes([0x0C, len(raw)]) + raw


def make_cert(version: str = "0.7.1", **change: str) -> x509.Certificate:
    """A certificate carrying a genuine release's Fulcio extensions, with `change` applied."""
    ident = f"{V.WORKFLOW}@refs/tags/v{version}"
    fields = {
        "san": ident,
        "issuer_v1": V.ISSUER,
        "issuer_v2": V.ISSUER,
        "build_config": ident,
        "source_ref": f"refs/tags/v{version}",
        "repository_id": V.REPOSITORY_ID,
        "owner_id": V.OWNER_ID,
        "runner": "github-hosted",
        "trigger": "push",
        "source_digest": RELEASES.get(version, RELEASES["0.7.1"])[1],
        **change,
    }
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "test")])
    now = dt.datetime.now(dt.UTC)
    raw = {
        "1": fields["issuer_v1"].encode(),  # v1: the bare bytes
        "8": _utf8(fields["issuer_v2"]),     # v2: DER UTF8String
        "18": _utf8(fields["build_config"]),
        "14": _utf8(fields["source_ref"]),
        "15": _utf8(fields["repository_id"]),
        "17": _utf8(fields["owner_id"]),
        "11": _utf8(fields["runner"]),
        "20": _utf8(fields["trigger"]),
        "13": _utf8(fields["source_digest"]),
    }
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
               .public_key(key.public_key()).serial_number(1)
               .not_valid_before(now).not_valid_after(now + dt.timedelta(minutes=10))
               .add_extension(x509.SubjectAlternativeName(
                   [x509.UniformResourceIdentifier(fields["san"])]), critical=True))
    for arc, value in raw.items():
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(OID + arc), value), critical=False)
    return builder.sign(key, hashes.SHA256())


@pytest.mark.parametrize("version", ["0.7.1", "0.8.0"])
def test_v3_the_policy_passes_the_real_certificates_and_a_faithful_copy(version):
    real = Bundle.from_json(bundle(version)).signing_certificate
    V.policy_for(version).verify(real)
    V.policy_for(version).verify(make_cert(version))
    with pytest.raises(VerificationError):
        V.policy_for("0.7.0").verify(real)


OTHER_WORKFLOW = f"{V.SOURCE}/.github/workflows/tests.yml@refs/tags/v0.7.1"
FORK = "https://github.com/someone-else/household-spend-tracker/.github/workflows/release.yml@refs/tags/v0.7.1"

WRONG = {
    "wrong workflow (SAN)": ({"san": OTHER_WORKFLOW}, "SANs do not match"),
    "wrong workflow (.18)": ({"build_config": OTHER_WORKFLOW}, "OIDCBuildConfigURI"),
    "wrong repository (SAN and .18)": ({"san": FORK, "build_config": FORK}, "SANs do not match"),
    "wrong repository id": ({"repository_id": "1404177157"}, "OIDCSourceRepositoryIdentifier"),
    "wrong owner id": ({"owner_id": "335945573"}, "OIDCSourceRepositoryOwnerIdentifier"),
    "self-hosted runner": ({"runner": "self-hosted"}, "OIDCRunnerEnvironment"),
    "manual trigger": ({"trigger": "workflow_dispatch"}, "OIDCBuildTrigger"),
    "branch, not tag": ({"source_ref": "refs/heads/main"}, "OIDCSourceRepositoryRef"),
    "another issuer (.1)": ({"issuer_v1": "https://accounts.google.com"}, "OIDCIssuer does not match"),
    "another issuer (.8)": ({"issuer_v2": "https://accounts.google.com"}, "OIDCIssuerV2 does not match"),
}


@pytest.mark.parametrize("case", sorted(WRONG))
def test_v3_each_wrong_certificate_field_is_refused(case):
    change, message = WRONG[case]
    with pytest.raises(VerificationError) as e:
        V.policy_for("0.7.1").verify(make_cert("0.7.1", **change))
    assert message in str(e.value)


@pytest.mark.parametrize("version", ["0.7.0", "0.7.1"])
def test_v3_the_real_certificate_is_checked_against_the_baked_in_ids(monkeypatch, version):
    monkeypatch.setattr(V, "REPOSITORY_ID", "1404177157")
    with pytest.raises(V.Refused) as e:
        run(version)
    assert "OIDCSourceRepositoryIdentifier" in e.value.detail and "got 1404177156" in e.value.detail


@pytest.mark.parametrize("version", ["0.7.1", "0.8.0"])
def test_v3_the_real_certificate_is_checked_against_the_owner_id(monkeypatch, version):
    monkeypatch.setattr(V, "OWNER_ID", "335945573")
    with pytest.raises(V.Refused) as e:
        run(version)
    assert "OIDCSourceRepositoryOwnerIdentifier" in e.value.detail and "got 335945572" in e.value.detail


# --------------------------------------------------------------------------- #
# The signed statement (rules 1, 5, 6, 7), and the index digest (C6)
# --------------------------------------------------------------------------- #


def statement(version: str = "0.7.1") -> dict:
    envelope = json.loads(bundle(version))["dsseEnvelope"]
    return json.loads(base64.b64decode(envelope["payload"]))


def check(st: dict, digest: str | None = None, version: str = "0.7.1", repository: str = APP,
          payload_type: str = V.PAYLOAD_TYPE) -> str:
    return V.check_statement(payload_type, json.dumps(st).encode(), repository,
                             digest or RELEASES[version][0], version)


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
def test_the_real_statements_name_their_commit(version):
    assert check(statement(version), version=version) == RELEASES[version][1]


INDEX = "sha256:" + "1" * 64
PLATFORM = "sha256:" + "2" * 64


def index_statement(version: str = "0.7.1") -> dict:
    """How #181's pipeline attests: the subject is the multi-arch index, not a platform image."""
    st = statement(version)
    st["subject"] = [{"name": APP, "digest": {"sha256": INDEX.removeprefix("sha256:")}}]
    return st


@pytest.mark.parametrize("version", ["0.7.1", "0.8.0"])
def test_an_index_digest_verifies_as_its_subject_and_a_platform_digest_does_not(version):
    assert check(index_statement(version), digest=INDEX, version=version) == RELEASES[version][1]
    with pytest.raises(V.Refused) as e:
        check(index_statement(version), digest=PLATFORM, version=version)
    assert e.value.rule == "subject"


def _set(doc: dict, path: str, value) -> dict:
    keys = path.split(".")
    for key in keys[:-1]:
        doc = doc[int(key)] if isinstance(doc, list) else doc[key]
    doc[keys[-1]] = value
    return doc


GH = "predicate.buildDefinition.internalParameters.github"
BAD_STATEMENTS = {
    "another predicate": ("predicateType", "https://spdx.dev/Document", "predicate"),
    "not a statement": ("_type", "https://in-toto.io/Statement/v0.1", "statement"),
    "provenance repository id": (f"{GH}.repository_id", "1404177157", "provenance"),
    "provenance owner id": (f"{GH}.repository_owner_id", "335945573", "provenance"),
    "provenance runner": (f"{GH}.runner_environment", "self-hosted", "provenance"),
    "provenance trigger": (f"{GH}.event_name", "workflow_dispatch", "provenance"),
    "provenance workflow ref": ("predicate.buildDefinition.externalParameters.workflow.ref",
                                "refs/tags/v0.7.0", "provenance"),
    "provenance workflow path": ("predicate.buildDefinition.externalParameters.workflow.path",
                                 ".github/workflows/tests.yml", "provenance"),
    "provenance builder": ("predicate.runDetails.builder.id", f"{V.WORKFLOW}@refs/heads/main", "provenance"),
    "no commit": ("predicate.buildDefinition.resolvedDependencies", [], "provenance"),
    "short commit": ("predicate.buildDefinition.resolvedDependencies.0.digest.gitCommit", "987afef", "provenance"),
    "subject named as the updater": ("subject.0.name", UPDATER, "subject"),
}


@pytest.mark.parametrize("case", sorted(BAD_STATEMENTS))
def test_a_statement_that_disagrees_with_its_certificate_is_refused(case):
    path, value, rule = BAD_STATEMENTS[case]
    st = statement("0.7.1")
    check(copy.deepcopy(st))  # the unchanged statement passes
    _set(st, path, value)
    with pytest.raises(V.Refused) as e:
        check(st)
    assert e.value.rule == rule


@pytest.mark.parametrize("payload_type", ["application/json", "text/plain"])
def test_a_payload_that_is_not_in_toto_is_refused(payload_type):
    with pytest.raises(V.Refused) as e:
        check(statement(), payload_type=payload_type)
    assert e.value.rule == "statement"


# --------------------------------------------------------------------------- #
# V4: a changed digest, and a tampered bundle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", ["0.7.0", "0.8.0"])
def test_v4_a_digest_one_character_off_is_refused(version):
    digest = RELEASES[version][0]
    changed = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    with pytest.raises(V.Refused) as e:
        run(version, digest=changed)
    assert e.value.rule == "subject"


def _flip(b64: str) -> str:
    raw = bytearray(base64.b64decode(b64))
    raw[0] ^= 1
    return base64.b64encode(bytes(raw)).decode()


def _entry(b: dict) -> dict:
    return b["verificationMaterial"]["tlogEntries"][0]


TAMPER = {
    "inclusion proof hash flipped": (
        lambda b: _entry(b)["inclusionProof"]["hashes"].__setitem__(0, _flip(_entry(b)["inclusionProof"]["hashes"][0])),
        "inclusion proof"),
    "checkpoint signature mangled": (
        lambda b: _entry(b)["inclusionProof"]["checkpoint"].__setitem__(
            "envelope", _entry(b)["inclusionProof"]["checkpoint"]["envelope"][:-8] + "AAAAAAA\n"),
        "checkpoint"),
    "one payload byte changed": (
        lambda b: b["dsseEnvelope"].__setitem__("payload", _flip(b["dsseEnvelope"]["payload"])),
        "signature"),
    "log entry removed": (
        lambda b: b["verificationMaterial"].__setitem__("tlogEntries", []),
        "log entry"),
    "integratedTime past the certificate": (
        lambda b: _entry(b).__setitem__("integratedTime", str(int(_entry(b)["integratedTime"]) + 86400)),
        "expired"),
    "signature replaced by another release's": (
        lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__(
            "sig", json.loads(bundle("0.7.0"))["dsseEnvelope"]["signatures"][0]["sig"]),
        "signature"),
}


@pytest.mark.parametrize("version", ["0.7.1", "0.8.0"])
@pytest.mark.parametrize("case", sorted(TAMPER))
def test_v4_a_tampered_bundle_is_refused(version, case):
    mutate, words = TAMPER[case]
    data = json.loads(bundle(version))
    mutate(data)
    with pytest.raises(V.Refused) as e:
        run(version, data=json.dumps(data).encode())
    assert e.value.rule == "signature"
    assert words in e.value.detail.lower()


# --------------------------------------------------------------------------- #
# V5: any exception means refused, and nothing skips the check
# --------------------------------------------------------------------------- #


class Boom(Exception):
    pass


def boom(*a, **k):
    raise Boom("something nobody planned for")


@pytest.mark.parametrize("where", ["fetch", "tuf_root"])
def test_v5_an_unexpected_exception_anywhere_is_a_refusal(where):
    # A TUF root that fails with anything but a TUF or cache error is not a
    # reason to fall back: it is a refusal.
    kw = {"fetch": boom} if where == "fetch" else {"tuf_root": boom}
    with pytest.raises(V.Refused) as e:
        V.verify(APP, RELEASES["0.7.1"][0], "0.7.1", **{"fetch": lambda r, d: [bundle("0.7.1")], **kw})
    assert (e.value.rule, e.value.detail) == ("error", "Boom: something nobody planned for")


@pytest.mark.parametrize("data", [b"", b"not json", b"{}", b'{"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}'])
def test_v5_a_bundle_that_does_not_parse_is_a_refusal(data):
    with pytest.raises(V.Refused) as e:
        run("0.7.1", data=data)
    assert e.value.rule == "signature"


@pytest.mark.parametrize("bundles", [[], None])
def test_v5_no_bundle_at_all_is_a_refusal(bundles):
    def fetch(r, d):
        if bundles is None:
            raise V.Refused("attestation", "the registry has no attestation for this image")
        return bundles
    with pytest.raises(V.Refused) as e:
        V.verify(APP, RELEASES["0.7.1"][0], "0.7.1", fetch=fetch, tuf_root=tuf_unreachable)
    assert e.value.rule == "attestation"


def test_v5_there_is_no_parameter_or_variable_that_skips_verification():
    assert list(inspect.signature(V.verify).parameters) == ["repository", "digest", "version", "fetch", "tuf_root"]
    # Nor an environment variable: the module does not import `os` at all.
    assert "os" not in _imports(ROOT / "updater" / "verify.py")


@pytest.mark.parametrize("repository", ["ghcr.io/someone-else/household-spend-tracker", "docker.io/library/python"])
def test_v5_an_image_from_anywhere_else_is_refused_before_any_fetch(repository):
    calls = []
    with pytest.raises(V.Refused) as e:
        V.verify(repository, RELEASES["0.7.1"][0], "0.7.1", fetch=lambda r, d: calls.append(r) or [],
                 tuf_root=tuf_unreachable)
    assert (e.value.rule, calls) == ("repository", [])


@pytest.mark.parametrize("digest", ["sha256:ABC", "sha512:" + "0" * 128,
                                    RELEASES["0.7.1"][0].removeprefix("sha256:")])
def test_v5_a_malformed_digest_is_refused_before_any_fetch(digest):
    calls = []
    with pytest.raises(V.Refused) as e:
        V.verify(APP, digest, "0.7.1", fetch=lambda r, d: calls.append(d) or [], tuf_root=tuf_unreachable)
    assert (e.value.rule, calls) == ("digest", [])


# --------------------------------------------------------------------------- #
# V6: a prerelease is never a release
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", ["0.7.1-rc1", "0.8.0-beta.2", "v0.7.1", "0.7", "0.7.1+build", "00.7.1"])
def test_v6_a_version_with_a_suffix_or_another_shape_is_refused_before_any_fetch(version):
    calls = []
    with pytest.raises(V.Refused) as e:
        V.verify(APP, RELEASES["0.7.1"][0], version, fetch=lambda r, d: calls.append(d) or [bundle("0.7.1")],
                 tuf_root=tuf_unreachable)
    assert (e.value.rule, calls) == ("version", [])


@pytest.mark.parametrize("released", ["0.7.1", "0.8.0"])
def test_v6_a_certificate_for_an_rc_tag_does_not_pass_the_release_policy(released):
    rc = f"{V.WORKFLOW}@refs/tags/v{released}-rc1"
    cert = make_cert(released, san=rc, build_config=rc, source_ref=f"refs/tags/v{released}-rc1")
    with pytest.raises(VerificationError) as e:
        V.policy_for(released).verify(cert)
    assert f"refs/tags/v{released}" in str(e.value)
    with pytest.raises(V.Refused):
        V.policy_for(f"{released}-rc1")


# --------------------------------------------------------------------------- #
# Labels on the pulled platform image (4.4 P5, C8)
# --------------------------------------------------------------------------- #


def verified(version: str) -> V.Verified:
    digest, commit = RELEASES[version]
    return V.Verified(repository=APP, digest=digest, version=version, commit=commit, root="embedded")


@pytest.mark.parametrize("label", ["0.7.1", "v0.7.1"])
def test_labels_before_and_after_c8_agree_with_the_attestation(label):
    V.labels_agree(verified("0.7.1"), {V.REVISION_LABEL: RELEASES["0.7.1"][1], V.VERSION_LABEL: label})


@pytest.mark.parametrize(("labels", "what"), [
    ({V.REVISION_LABEL: RELEASES["0.7.0"][1], V.VERSION_LABEL: "0.7.1"}, "revision"),
    ({V.REVISION_LABEL: RELEASES["0.7.1"][1], V.VERSION_LABEL: "v0.7.0"}, "version"),
    ({V.REVISION_LABEL: RELEASES["0.7.1"][1], V.VERSION_LABEL: "vv0.7.1"}, "version"),
    ({V.REVISION_LABEL: RELEASES["0.7.1"][1], V.VERSION_LABEL: "0.7.1-rc1"}, "version"),
    ({V.REVISION_LABEL: RELEASES["0.7.1"][1]}, "version"),
    ({}, "revision"),
])
def test_labels_that_disagree_with_the_attestation_are_refused(labels, what):
    with pytest.raises(V.Refused) as e:
        V.labels_agree(verified("0.7.1"), labels)
    assert e.value.rule == "labels" and f"{what} label" in e.value.detail


def test_the_verified_record_is_what_a_prepare_report_carries():
    assert verified("0.8.0").to_dict() == {
        "repository": APP, "digest": RELEASES["0.8.0"][0], "version": "0.8.0",
        "commit": RELEASES["0.8.0"][1], "root": "embedded",
    }


# --------------------------------------------------------------------------- #
# The embedded root, the two repositories, and what imports sigstore
# --------------------------------------------------------------------------- #

@pytest.mark.repo_wide
def test_the_embedded_root_is_the_pinned_wheels_copy():
    """7.5: each bump of the sigstore pin brings a newer root, and this says to commit it."""
    wheel = (importlib.resources.files("sigstore") / "_store" / "https%3A%2F%2Ftuf-repo-cdn.sigstore.dev"
             / "trusted_root.json").read_bytes()
    assert json.loads(V.EMBEDDED_ROOT.read_bytes()) == json.loads(wheel), (
        "updater/trusted_root.json is not the sigstore wheel's; copy it from sigstore/_store/")


def test_the_embedded_root_carries_both_rekor_logs_in_use():
    """7.2 rule 2: the time anchor is Rekor's, so every log that signed a release must be in the root."""
    root = json.loads(V.EMBEDDED_ROOT.read_bytes())
    urls = {t["baseUrl"] for t in root["tlogs"]}
    assert {"https://rekor.sigstore.dev", "https://log2025-1.rekor.sigstore.dev"} <= urls


def test_the_two_repositories_are_the_engine_clients_two():
    from updater import engine
    assert set(V.REPOSITORIES) == set(engine.REPOSITORIES)
    assert [V.REPOSITORIES[r] for r in engine.REPOSITORIES] == [r.removeprefix("ghcr.io/") for r in engine.REPOSITORIES]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_only_the_verifier_imports_outside_the_standard_library():
    outside = {p.name: _imports(p) - set(sys.stdlib_module_names) - {"updater"}
               for p in sorted((ROOT / "updater").glob("*.py"))}
    assert outside.pop("verify.py") == {"certifi", "sigstore"}
    assert {name: mods for name, mods in outside.items() if mods} == {}


@pytest.mark.repo_wide
def test_sigstore_stays_out_of_the_app_and_its_runtime_lock():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "sigstore" not in runtime and "\ntuf==" not in runtime
    assert "sigstore==" in (ROOT / "requirements-updater.txt").read_text(encoding="utf-8")
    users = [p for p in (ROOT / "app").rglob("*.py") if "sigstore" in _imports(p) or "updater" in _imports(p)]
    assert users == []
