"""Password hashing with scrypt (hashlib.scrypt, RFC 7914) — a standard memory-hard KDF from the
Python standard library. Argon2 / bcrypt are not installed in this project and no dependency is
added for them; the stored format carries its parameters so a future migration can rehash.

Format: scrypt$N$r$p$<salt b64>$<hash b64>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P, DKLEN = 2 ** 15, 8, 1, 32
MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LEN = 12


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN, maxmem=MAXMEM)
    return "scrypt${}${}${}${}${}".format(N, R, P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(str(password).encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                            dklen=len(expected), maxmem=MAXMEM)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# A real hash of a random throwaway password, so a login for an unknown user costs the same time.
_DUMMY = None


def dummy_verify(password: str) -> None:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY)
