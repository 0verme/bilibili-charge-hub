import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()
SESSION_TTL = timedelta(days=7)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hash.verify(password, encoded)


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_expiry() -> datetime:
    return datetime.now(UTC) + SESSION_TTL
