"""Encryption of the provider keys orgs save (`api_keys.ciphertext`).

Protects against a database dump (a lost backup, replica access). It does not
protect against someone who has both the database and the environment, which is
why the master key lives in the environment, never in a table.

Format: `v1:` + a Fernet token. The version prefix is what lets old rows be read
after the scheme changes, and makes a master-key rotation possible without
guessing.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

PREFIX = b"v1:"

_MISSING = (
    "API_KEYS_ENCRYPTION_KEY is missing from the environment, so provider keys can be "
    "neither written nor read. Generate one with:\n"
    '  uv run python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)

_INVALID = (
    "API_KEYS_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes). "
    "Generate another with the command in .env.example."
)

_fernet_cache: Fernet | None = None


def configured() -> bool:
    return bool(settings.api_keys_encryption_key)


def _fernet() -> Fernet:
    # Built lazily, not at import: an install that never saved a provider key must
    # not fail to start because the master key is empty.
    global _fernet_cache
    if _fernet_cache is None:
        if not configured():
            raise RuntimeError(_MISSING)
        try:
            _fernet_cache = Fernet(settings.api_keys_encryption_key.encode())
        except (ValueError, TypeError) as cause:
            raise RuntimeError(_INVALID) from cause
    return _fernet_cache


def encrypt(secret: str) -> bytes:
    return PREFIX + _fernet().encrypt(secret.encode())


def decrypt(blob: bytes) -> str:
    """Raises `RuntimeError` if the row was written with another master key; the only
    honest fix then is saving the key again."""
    if not blob.startswith(PREFIX):
        raise RuntimeError("The saved key lacks the expected version prefix.")
    try:
        return _fernet().decrypt(blob[len(PREFIX) :]).decode()
    except InvalidToken as cause:
        raise RuntimeError(
            "The saved key cannot be decrypted with API_KEYS_ENCRYPTION_KEY. If the master "
            "key changed, the provider key must be saved again."
        ) from cause
