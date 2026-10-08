"""Reading an attestation bundle out of a registry (design notes 7.1, 7.2 rule 1).

A fake ghcr served on loopback: the anonymous token, the `sha256-<hex>`
fallback tag holding an OCI index, the bundle's manifest, and the blob behind
a redirect to a second host, as ghcr sends it to its blob storage. It serves
the real recorded bundles of two releases, so a fetch that works here hands
`verify` the very bytes the registry holds.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sigstore.errors import TUFError

from updater import verify as V

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "updater" / "attestations"
APP = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPDATER = "ghcr.io/mariolonghi-com/household-spend-tracker-updater"
DIGESTS = {
    "0.7.0": "sha256:cf94c787efefa8981d0984c73ebeeb573d5a6720e3f13743ea5eecbc78aaf72b",
    "0.7.1": "sha256:243ca238dcc002a29689eeafa211f94ac3d55c5b22dce98d2dada1bdb0c3da21",
}
TOKEN = "anonymous-pull-token"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def as_json(doc: object) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode()


class Store:
    """What the fake registry holds: per path, manifests by reference and blobs by digest."""

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.blobs: dict[tuple[str, str], bytes] = {}
        #: Each request: (host, path, Authorization header or None).
        self.seen: list[tuple[str, str, str | None]] = []
        self.blob_redirect: str | None = None  # where /blobs/ sends a client; set by the fixture
        self.redirect_manifests = False
        self.blob_host_redirects = False

    def publish(self, path: str, digest: str, bundle: bytes, *, subject: str | None = None,
                artifact_type: str = "application/vnd.dev.sigstore.bundle.v0.3+json") -> dict:
        """The layout attest-build-provenance pushes, with `bundle` as the layer."""
        manifest = as_json({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": artifact_type,
            "config": {"mediaType": "application/vnd.oci.empty.v1+json", "digest": sha(b"{}"), "size": 2},
            "layers": [{"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                        "digest": sha(bundle), "size": len(bundle)}],
            "subject": {"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                        "digest": subject or digest, "size": 826},
        })
        index = as_json({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "digest": sha(manifest), "size": len(manifest), "artifactType": artifact_type,
                           "annotations": {"dev.sigstore.bundle.predicateType": "https://slsa.dev/provenance/v1"}}],
        })
        self.manifests[(path, "sha256-" + digest.removeprefix("sha256:"))] = index
        self.manifests[(path, sha(manifest))] = manifest
        self.blobs[(path, sha(bundle))] = bundle
        return {"index": index, "manifest": manifest, "layer": sha(bundle)}


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def registry():
    store = Store()

    class Registry(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            store.seen.append(("registry", self.path, self.headers.get("Authorization")))
            if self.path.startswith("/token?scope=repository:"):
                return self._send(200, as_json({"token": TOKEN}))
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401)
            parts = self.path.split("/")
            # /v2/<owner>/<name>/(manifests|blobs)/<ref>
            if len(parts) == 6 and parts[1] == "v2":
                path, kind, ref = f"{parts[2]}/{parts[3]}", parts[4], parts[5]
                if kind == "manifests" and store.redirect_manifests:
                    return self._send(307, headers={"Location": store.blob_redirect + f"/{path}/{ref}"})
                if kind == "manifests" and (path, ref) in store.manifests:
                    return self._send(200, store.manifests[(path, ref)],
                                      {"Content-Type": "application/vnd.oci.image.index.v1+json"})
                if kind == "blobs" and (path, ref) in store.blobs:
                    return self._send(307, headers={"Location": store.blob_redirect + f"/{path}/{ref}"})
            return self._send(404, as_json({"errors": [{"code": "MANIFEST_UNKNOWN"}]}))

    class Blobs(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            store.seen.append(("blobs", self.path, self.headers.get("Authorization")))
            if store.blob_host_redirects:
                self.send_response(307)
                self.send_header("Location", store.blob_redirect + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            _, owner, name, ref = self.path.split("/")
            body = store.blobs.get((f"{owner}/{name}", ref))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    reg, reg_port = _serve(Registry)
    blobs, blob_port = _serve(Blobs)
    # Two hosts for one machine: the registry is 127.0.0.1, its blob store `localhost`.
    store.blob_redirect = f"http://localhost:{blob_port}"
    store.client = V.Registry(base=f"http://127.0.0.1:{reg_port}", blob_hosts=("localhost",), timeout=5)
    for version, digest in DIGESTS.items():
        store.publish(V.REPOSITORIES[APP], digest, (FIXTURES / f"{version}.sigstore.json").read_bytes())
    yield store
    for server in (reg, blobs):
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# What a fetch returns
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", sorted(DIGESTS))
def test_the_fetch_returns_the_bundle_exactly_as_the_registry_holds_it(registry, version):
    got = V.fetch_bundles(APP, DIGESTS[version], registry.client)
    assert got == [(FIXTURES / f"{version}.sigstore.json").read_bytes()]


@pytest.mark.parametrize("version", sorted(DIGESTS))
def test_a_fetched_bundle_verifies_end_to_end(registry, version):
    def offline():
        raise TUFError("unreachable")
    got = V.verify(APP, DIGESTS[version], version,
                   fetch=lambda r, d: V.fetch_bundles(r, d, registry.client), tuf_root=offline)
    assert (got.version, got.digest, got.root) == (version, DIGESTS[version], "embedded")


def test_the_token_goes_to_the_registry_and_never_to_the_blob_host(registry):
    V.fetch_bundles(APP, DIGESTS["0.7.1"], registry.client)
    hosts = [(host, auth) for host, _, auth in registry.seen]
    assert hosts == [("registry", None), ("registry", f"Bearer {TOKEN}"), ("registry", f"Bearer {TOKEN}"),
                     ("registry", f"Bearer {TOKEN}"), ("blobs", None)]
    assert registry.seen[1][1] == "/v2/mariolonghi-com/household-spend-tracker/manifests/sha256-" \
        + DIGESTS["0.7.1"].removeprefix("sha256:")


def test_the_updater_repository_is_read_from_its_own_path(registry):
    bundle = (FIXTURES / "0.7.1.sigstore.json").read_bytes()
    digest = "sha256:" + "3" * 64
    registry.publish(V.REPOSITORIES[UPDATER], digest, bundle)
    assert V.fetch_bundles(UPDATER, digest, registry.client) == [bundle]
    assert "/v2/mariolonghi-com/household-spend-tracker-updater/manifests/sha256-" + "3" * 64 \
        in [p for _, p, _ in registry.seen]
    with pytest.raises(V.Refused):
        V.fetch_bundles(APP, digest, registry.client)  # not in the app's repository


# --------------------------------------------------------------------------- #
# What a fetch refuses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", sorted(DIGESTS))
def test_a_blob_that_does_not_hash_to_its_digest_is_refused(registry, version):
    path = V.REPOSITORIES[APP]
    layer = next(k for k in registry.blobs if k[0] == path and registry.blobs[k] ==
                 (FIXTURES / f"{version}.sigstore.json").read_bytes())
    registry.blobs[layer] = registry.blobs[layer].replace(b"tlogEntries", b"tlogEntriez")
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS[version], registry.client)
    assert e.value.rule == "attestation" and f"not {layer[1]}" in e.value.detail


def test_a_manifest_that_does_not_hash_to_its_digest_is_refused(registry):
    path = V.REPOSITORIES[APP]
    index = json.loads(registry.manifests[(path, "sha256-" + DIGESTS["0.7.0"].removeprefix("sha256:"))])
    key = (path, index["manifests"][0]["digest"])
    registry.manifests[key] = registry.manifests[key].replace(b'"schemaVersion":2', b'"schemaVersion":2 ')
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.0"], registry.client)
    assert "the bundle's manifest hashes to" in e.value.detail


@pytest.mark.parametrize("version", sorted(DIGESTS))
def test_a_bundle_manifest_about_another_image_is_refused(registry, version):
    other = DIGESTS["0.7.1" if version == "0.7.0" else "0.7.0"]
    registry.publish(V.REPOSITORIES[APP], DIGESTS[version], (FIXTURES / f"{version}.sigstore.json").read_bytes(),
                     subject=other)
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS[version], registry.client)
    assert e.value.detail == "the bundle's manifest is about another image"


@pytest.mark.parametrize("artifact_type", ["application/spdx+json", "application/vnd.in-toto+json"])
def test_an_index_with_no_sigstore_bundle_is_refused(registry, artifact_type):
    digest = "sha256:" + "4" * 64
    registry.publish(V.REPOSITORIES[APP], digest, b"{}", artifact_type=artifact_type)
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, digest, registry.client)
    assert e.value.detail == "the registry has no attestation bundle for this image"


@pytest.mark.parametrize("digest", ["sha256:" + "5" * 64, "sha256:" + "6" * 64])
def test_an_image_with_no_fallback_tag_is_refused(registry, digest):
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, digest, registry.client)
    assert (e.value.rule, e.value.detail) == ("attestation", "the registry has no attestation for this image")


@pytest.mark.parametrize("host", ["127.0.0.1", "example.invalid"])
def test_a_blob_redirect_anywhere_but_the_blob_host_is_not_followed(registry, host):
    registry.blob_redirect = registry.blob_redirect.replace("localhost", host)
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.1"], registry.client)
    assert e.value.detail == f"the registry redirected to http://{host}"
    assert [h for h, _, _ in registry.seen].count("blobs") == 0


def test_a_blob_redirect_to_another_scheme_is_not_followed(registry):
    registry.blob_redirect = registry.blob_redirect.replace("http://", "https://")
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.0"], registry.client)
    assert e.value.detail == "the registry redirected to https://localhost"


def test_a_manifest_redirect_is_never_followed(registry):
    registry.redirect_manifests = True
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.1"], registry.client)
    assert e.value.detail == "the registry answered 307"
    assert [h for h, _, _ in registry.seen].count("blobs") == 0


def test_the_blob_host_may_not_redirect_again(registry):
    registry.blob_host_redirects = True
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.1"], registry.client)
    assert e.value.detail == "the blob host answered 307"
    assert [h for h, _, _ in registry.seen].count("blobs") == 1


def test_an_unreachable_blob_host_is_its_own_refusal(registry):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    registry.blob_redirect = f"http://localhost:{port}"
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.0"], registry.client)
    assert e.value.rule == "unreachable" and "blob host" in e.value.detail


@pytest.mark.parametrize("cap", [100, 11_000])
def test_a_blob_larger_than_the_cap_is_refused(registry, monkeypatch, cap):
    monkeypatch.setattr(V, "MAX_BUNDLE", cap)
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.1"], registry.client)
    assert f"at most {cap}" in e.value.detail


def test_an_unreachable_registry_is_its_own_refusal():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(V.Refused) as e:
        V.fetch_bundles(APP, DIGESTS["0.7.1"], V.Registry(base=f"http://127.0.0.1:{port}", timeout=2))
    assert e.value.rule == "unreachable"


@pytest.mark.parametrize("body", [b"not json", b'{"token": ""}'])
def test_a_registry_that_gives_no_token_is_refused(body):
    class NoToken(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server, port = _serve(NoToken)
    try:
        with pytest.raises(V.Refused) as e:
            V.fetch_bundles(APP, DIGESTS["0.7.1"], V.Registry(base=f"http://127.0.0.1:{port}", timeout=5))
        assert e.value.rule == "attestation" and "token" in e.value.detail
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# Resolving a release to a digest without pulling (4.4 P2, P3)
# --------------------------------------------------------------------------- #


def _platform_manifest(layers: list[int], config: int = 1500) -> bytes:
    return as_json({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": sha(b"c"), "size": config},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": sha(bytes([i])), "size": n}
                   for i, n in enumerate(layers)],
    })  # fmt: skip


def _release(store: Store, path: str, version: str, platforms: dict[str, bytes]) -> bytes:
    """A multi-arch release as release.yml pushes it: an index at the version's tag."""
    index = as_json({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            *({"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": sha(body), "size": len(body),
               "platform": {"os": "linux", "architecture": arch}} for arch, body in platforms.items()),
            # buildx's attestation manifest sits in the index too, as unknown/unknown.
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": sha(b"att"), "size": 3,
             "platform": {"os": "unknown", "architecture": "unknown"}},
        ],
    })  # fmt: skip
    store.manifests[(path, version)] = index
    for body in platforms.values():
        store.manifests[(path, sha(body))] = body
    return index


@pytest.mark.parametrize("arch,layers", [("amd64", [40_000_000, 2_000_000]), ("arm64", [38_000_000, 1_900_000])])
def test_a_release_resolves_to_its_index_digest_and_the_platform_image_size(registry, arch, layers):
    path = V.REPOSITORIES[APP]
    bodies = {"amd64": _platform_manifest([40_000_000, 2_000_000]), "arm64": _platform_manifest([38_000_000, 1_900_000])}
    index = _release(registry, path, "0.8.0", bodies)
    found = V.resolve(APP, "0.8.0", arch, registry.client)
    assert found.digest == sha(index) and found.platform == f"linux/{arch}"
    assert found.size == sum(layers) + 1500
    # Two reads: the tag, then the platform manifest by its digest. Nothing pulled.
    reads = [p for _, p, _ in registry.seen if "/manifests/" in p]
    assert reads == [f"/v2/{path}/manifests/0.8.0", f"/v2/{path}/manifests/{sha(bodies[arch])}"]


def test_a_single_platform_release_resolves_to_its_manifest(registry):
    path = V.REPOSITORIES[UPDATER]
    body = _platform_manifest([10_000_000])
    registry.manifests[(path, "0.8.0")] = body
    found = V.resolve(UPDATER, "0.8.0", "amd64", registry.client)
    assert found.digest == sha(body) and found.size == 10_001_500 and found.platform == "single"


def test_a_release_without_the_machines_platform_is_refused(registry):
    _release(registry, V.REPOSITORIES[APP], "0.8.0", {"amd64": _platform_manifest([1])})
    with pytest.raises(V.Refused) as e:
        V.resolve(APP, "0.8.0", "arm64", registry.client)
    assert e.value.rule == "platform"


def test_a_platform_manifest_that_does_not_hash_to_its_digest_is_refused(registry):
    path = V.REPOSITORIES[APP]
    body = _platform_manifest([1])
    _release(registry, path, "0.8.0", {"amd64": body})
    registry.manifests[(path, sha(body))] = _platform_manifest([2])
    with pytest.raises(V.Refused, match="hashes to"):
        V.resolve(APP, "0.8.0", "amd64", registry.client)


def test_a_release_with_no_such_tag_or_a_prerelease_is_refused(registry):
    with pytest.raises(V.Refused) as missing:
        V.resolve(APP, "0.9.9", "amd64", registry.client)
    assert missing.value.rule == "release" and missing.value.detail == "the registry has no 0.9.9 of this image"
    with pytest.raises(V.Refused) as pre:
        V.resolve(APP, "0.9.0-rc1", "amd64", registry.client)
    assert pre.value.rule == "version"


def test_an_unreachable_registry_is_refused_as_unreachable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(V.Refused) as e:
        V.resolve(APP, "0.8.0", "amd64", V.Registry(base=f"http://127.0.0.1:{port}", timeout=2))
    assert e.value.rule == "unreachable"
