"""Password hashing for the client's local auth store.

Werkzeug-compatible `generate_password_hash` / `check_password_hash` built on
the Python stdlib only (`hashlib.pbkdf2_hmac` + `secrets`) — werkzeug itself
is not a dependency of this container and Article XI says no new dependencies
for what the stdlib already covers. The emitted format is werkzeug's
`pbkdf2:sha256:<iterations>$<salt>$<hex>` so hashes remain valid if werkzeug
is ever dropped in.

Only the HASH is ever stored (users/{email}/auth.json). The plaintext
password never touches disk or a log line (Article VII).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_METHOD = "pbkdf2:sha256"
_ITERATIONS = 600_000
_SALT_CHARS = 16


def generate_password_hash(password: str) -> str:
    """Hash a password into the werkzeug `pbkdf2:sha256:iter$salt$hex` format."""
    salt = secrets.token_hex(_SALT_CHARS // 2)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("utf-8"), _ITERATIONS)
    return f"{_METHOD}:{_ITERATIONS}${salt}${dk.hex()}"


def check_password_hash(pwhash: str, password: str) -> bool:
    """Constant-time check of a password against a stored hash.

    Returns False (never raises) on a malformed / empty / unsupported hash.
    """
    try:
        method, salt, hashval = (pwhash or "").split("$", 2)
        parts = method.split(":")
        if parts[0] != "pbkdf2" or parts[1] != "sha256":
            return False
        iterations = int(parts[2]) if len(parts) > 2 else 260_000
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), iterations)
        return hmac.compare_digest(dk.hex(), hashval)
    except Exception:
        return False
