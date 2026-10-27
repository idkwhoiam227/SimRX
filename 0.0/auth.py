"""Authentication helpers — stdlib only (hashlib.pbkdf2_hmac + hmac + json).

Same security model as the Cloudflare version of this project: PBKDF2
password hashing with a per-admin salt, signed stateless session tokens
carrying a token_version (so a password change / forced logout can
invalidate all existing sessions by bumping one integer), and lockout
after repeated failed login attempts.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

PBKDF2_ITERATIONS = 100_000
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12h
LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 15 * 60
COOKIE_NAME = "simrx_local_session"


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    computed = hash_password(password, salt)
    return hmac.compare_digest(computed, bytes.fromhex(expected_hash_hex))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def sign_session(payload: dict, secret: str) -> str:
    body = _b64url(json.dumps(payload).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def verify_session(token: str, secret: str):
    if not token or "." not in token:
        return None
    body, sig_b64 = token.rsplit(".", 1)
    expected_sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected_sig, _b64url_decode(sig_b64)):
            return None
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def make_session_cookie(token: str) -> str:
    return f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"


def clear_session_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def read_cookie(header_value: str, name: str = COOKIE_NAME):
    if not header_value:
        return None
    for part in header_value.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return None
