"""Key handling: one root key on disk, purpose-separated subkeys in memory.

The root key is generated on first boot rather than demanded from the operator
-- a key nobody had to invent beats one pasted out of an example. It is never
used directly: each purpose derives its own subkey, so a weakness in one use
cannot be carried into another.

**What losing it costs:** every TOTP secret. Sessions and trusted devices are
random values verified by database lookup, not signed tokens, and password
hashes are argon2, so a lost key does not by itself sign anyone out or lose
the ledger. But everyone re-enrols their authenticator (recovery mode, #287),
by way of a recovery code -- at a trusted browser too -- and a recovery code
signs its member out everywhere, forgets their trusted browsers and revokes
their agent keys (`service.redeem_recovery_code`). The original key put back
later returns none of those.
"""

from __future__ import annotations

import base64
import os
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import settings
from ..errors import ValidationError

_NONCE_BYTES = 12


def _root(secret_key: str | None = None) -> bytes:
    raw = (settings.secret_key if secret_key is None else secret_key).encode()
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # pragma: no cover - defensive
        raise ValidationError("SPENDTRACKER_SECRET_KEY is not valid base64") from exc
    if len(key) < 32:
        raise ValidationError("the secret key must be at least 32 bytes")
    return key


def _subkey(info: bytes, secret_key: str | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        _root(secret_key)
    )


def _totp_cipher(secret_key: str | None = None) -> AESGCM:
    return AESGCM(_subkey(b"spendtracker/totp-secret/v1", secret_key))


#: Each sealed payload gets its own derived key. One key for two unrelated blob
#: types meant a wizard blob and a half-finished sign-in were interchangeable
#: ciphertexts, and only the happy accident that their plaintexts do not parse
#: as each other kept that from being an escalation. Separation by `info` makes
#: it structural rather than lucky.
SETUP_BLOB = b"spendtracker/setup-blob/v1"
PENDING_SIGNIN = b"spendtracker/pending-signin/v1"


def _blob_cipher(info: bytes = SETUP_BLOB) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_subkey(info)))


def seal_totp_secret(secret: str, *, user_id: str) -> bytes:
    """Encrypt a TOTP secret, bound to the row it belongs to.

    The user id is authenticated associated data, so copying one user's
    ``totp_secret`` over another's fails to decrypt rather than silently
    working -- which a plain ciphertext would do.
    """
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + _totp_cipher().encrypt(nonce, secret.encode(), user_id.encode())


def open_totp_secret(sealed: bytes, *, user_id: str) -> str:
    nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
    try:
        return _totp_cipher().decrypt(nonce, ciphertext, user_id.encode()).decode()
    except Exception as exc:
        raise ValidationError(
            "that authenticator secret cannot be read with this server key"
        ) from exc


def key_opens_totp_secret(secret_key: str, sealed: bytes, *, user_id: str) -> bool:
    """Would `secret_key` -- not the one this process runs with -- open this?

    For `scripts/restore.py`, which has to decide whether the key it is about
    to put beside a restored ledger is that ledger's key, before it moves
    anything. The secret itself is never returned: the answer is the point, and
    the plaintext has no business in a restore's output.
    """
    nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
    try:
        _totp_cipher(secret_key).decrypt(nonce, ciphertext, user_id.encode())
    except Exception:  # noqa: BLE001 - a wrong key and a malformed key are one answer
        return False
    return True


def opens_totp_secret(sealed: bytes, *, user_id: str) -> bool:
    """Does the key this process runs with open this? Recovery mode asks (#287).

    Read-only, like the function above: the answer is wanted, the plaintext
    is not, and a refusal writes nothing -- the sealed secret has to survive
    byte for byte, because putting the original key back is what ends
    recovery mode for every member who has not re-enrolled.
    """
    return key_opens_totp_secret(settings.secret_key, bytes(sealed), user_id=user_id)


def seal_blob(payload: str, *, purpose: bytes = SETUP_BLOB) -> str:
    """Authenticated encryption for the setup wizard's in-flight state.

    Encryption, not just a signature: the payload carries an argon2 hash and a
    TOTP secret, and a signed-but-readable cookie would hand both to anyone who
    could read it.

    ``purpose`` picks the derived key, so a blob sealed for one use cannot be
    opened as another.
    """
    return _blob_cipher(purpose).encrypt(payload.encode()).decode()


def open_blob(
    token: str, *, max_age_seconds: int, at: int | None = None, purpose: bytes = SETUP_BLOB
) -> str:
    """Decrypt and check age in one step.

    ``at`` exists so expiry can be tested without waiting for it; production
    callers leave it alone. A blob sealed for a different ``purpose`` fails to
    decrypt, which is the point.
    """
    now = int(time.time()) if at is None else at
    try:
        return _blob_cipher(purpose).decrypt_at_time(token.encode(), max_age_seconds, now).decode()
    except InvalidToken as exc:
        raise ValidationError("that setup session has expired; start again") from exc
