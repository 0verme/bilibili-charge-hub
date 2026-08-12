import base64
import hashlib
import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.settings import get_settings


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("credential cannot be decrypted with the configured key") from exc

    def encrypt_json(self, value: dict[str, str]) -> str:
        return self.encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def decrypt_json(self, value: str) -> dict:
        decoded = json.loads(self.decrypt(value))
        if not isinstance(decoded, dict):
            raise ValueError("encrypted credential must contain a JSON object")
        return decoded


@lru_cache
def get_credential_cipher() -> CredentialCipher:
    settings = get_settings()
    if settings.credential_encryption_key:
        key = settings.credential_encryption_key.get_secret_value().encode()
    else:
        digest = hashlib.sha256(settings.app_secret_key.get_secret_value().encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return CredentialCipher(key)
