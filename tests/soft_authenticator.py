"""A software passkey authenticator, for the tests (#47 §2 "Tests").

It does what a platform authenticator does, with `cryptography` and `cbor2`
(which `webauthn` already brings): it makes a P-256 key per credential,
answers a creation request with a `none` attestation, and answers a request
for an assertion with a signature over the authenticator data and the hash of
the client data. Its output is the JSON a browser's `credential.toJSON()`
produces, so the server code under test sees exactly what a real client
sends.

It is not a test double of the server's own logic: nothing here is imported
from `app/`, and nothing it produces has been shaped to pass. A credential it
makes can be presented to the wrong user, with a replayed challenge, from the
wrong origin -- whatever a test wants to try.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

#: Flags in authenticator data (WebAuthn §6.1).
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40

#: The AAGUID iCloud Keychain reports. Any 16 bytes would do; a known one lets
#: a test assert the default name the server derives from it.
ICLOUD_KEYCHAIN = bytes.fromhex("fbfc3007154e4ecc8c0b6e020557d7bd")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class Credential:
    """One passkey held by the authenticator."""

    credential_id: bytes
    key: ec.EllipticCurvePrivateKey
    rp_id: str
    user_handle: bytes
    sign_count: int = 0

    @property
    def id(self) -> str:
        return b64url(self.credential_id)


@dataclass
class SoftAuthenticator:
    """A passkey provider. ``synced`` sets the backup flags, as a synced
    provider (iCloud Keychain, Google Password Manager) does; ``counts``
    whether it increments a signature counter, which synced providers mostly
    do not."""

    aaguid: bytes = ICLOUD_KEYCHAIN
    synced: bool = True
    counts: bool = True
    user_verifies: bool = True
    credentials: list[Credential] = field(default_factory=list)

    def _flags(self, *extra: int) -> int:
        flags = UP | (UV if self.user_verifies else 0)
        if self.synced:
            flags |= BE | BS
        for one in extra:
            flags |= one
        return flags

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()

    def create(self, options: dict, *, origin: str) -> dict:
        """Answer `navigator.credentials.create()` with these options."""
        rp_id = options["rp"]["id"]
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose_key = {
            1: 2,  # kty: EC2
            3: -7,  # alg: ES256
            -1: 1,  # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }
        credential = Credential(
            credential_id=os.urandom(32),
            key=key,
            rp_id=rp_id,
            user_handle=unb64url(options["user"]["id"]),
        )
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags(AT)])
            + struct.pack(">I", 0)
            + self.aaguid
            + struct.pack(">H", len(credential.credential_id))
            + credential.credential_id
            + cbor2.dumps(cose_key)
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        self.credentials.append(credential)
        return {
            "id": credential.id,
            "rawId": credential.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def get(self, options: dict, *, origin: str, credential: Credential | None = None) -> dict:
        """Answer `navigator.credentials.get()`. A discoverable credential for
        the RP ID unless the test names one -- including one made for another
        RP ID, which is what a test of a wrong-host passkey wants."""
        rp_id = options["rpId"]
        chosen = credential or next(c for c in self.credentials if c.rp_id == rp_id)
        if self.counts:
            chosen.sign_count += 1
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags()])
            + struct.pack(">I", chosen.sign_count)
        )
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = chosen.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": chosen.id,
            "rawId": chosen.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(chosen.user_handle),
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
