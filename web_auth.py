from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

PBKDF2_ITERATIONS = 310_000
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")


def normalize_login(value: str) -> str:
    return (value or "").strip()


def validate_login(value: str) -> str:
    login = normalize_login(value)
    if not _LOGIN_RE.fullmatch(login):
        raise ValueError("Логин: 3–40 символов, только латинские буквы, цифры, точка, _ и -")
    return login


def validate_password(value: str) -> str:
    password = value or ""
    if len(password) < 10:
        raise ValueError("Пароль должен содержать минимум 10 символов")
    if len(password) > 256:
        raise ValueError("Пароль слишком длинный")
    return password


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    password = validate_password(password)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = _unb64(salt_raw)
        expected = _unb64(digest_raw)
        actual = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False
