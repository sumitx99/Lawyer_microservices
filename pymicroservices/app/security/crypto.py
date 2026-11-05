"""Small crypto helpers: random tokens, hashing, signed tokens, TOTP, encryption at rest."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pymicroservices.app.utilities.secrets_control import get_settings


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _key(purpose: str) -> bytes:
    return hashlib.sha256(f"{purpose}:{get_settings().secret_key}".encode()).digest()


# Signed, expiring tokens (challenge state, CSRF, local upload links) ------------
def sign(payload: dict, purpose: str, ttl_seconds: int) -> str:
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_key(purpose), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def unsign(token: str, purpose: str) -> dict | None:
    try:
        raw, mac = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_key(purpose), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    if body.get("exp", 0) < time.time():
        return None
    return body


# Encryption at rest for stored refresh tokens ------------------------------------
def encrypt(plaintext: str) -> str:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key("enc")).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(ciphertext: str) -> str:
    raw = base64.b64decode(ciphertext)
    return AESGCM(_key("enc")).decrypt(raw[:12], raw[12:], None).decode()


# Password hashing (dev provider only; WorkOS owns passwords in production) ------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# TOTP (RFC 6238), used by the dev provider so MFA flows work locally -------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, at: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((at or time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def verify_totp(secret: str, code: str) -> bool:
    now = time.time()
    return any(hmac.compare_digest(totp(secret, now + drift * 30), code.strip()) for drift in (-1, 0, 1))


def totp_uri(secret: str, email: str, issuer: str = "Lexaior") -> str:
    return f"otpauth://totp/{issuer}:{email}?secret={secret}&issuer={issuer}"
