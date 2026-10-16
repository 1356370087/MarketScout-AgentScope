"""凭据加密边界。明文不出现在模型 repr、审计或配置导出中。"""

import base64
import hashlib
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    def __init__(self, encoded_key: str | None = None):
        encoded = encoded_key if encoded_key is not None else os.getenv("PLATFORM_SECRET_ENCRYPTION_KEY", "")
        try:
            key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError("平台凭据主密钥必须是 Base64 编码的 32 字节密钥") from exc
        if len(key) != 32:
            raise ValueError("PLATFORM_SECRET_ENCRYPTION_KEY 必须解码为 32 字节")
        self._cipher = AESGCM(key)
        self.key_id = hashlib.sha256(key).hexdigest()[:16]

    def encrypt(self, identity: str, secret: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher.encrypt(nonce, secret.encode(), identity.encode())

    def decrypt(self, identity: str, ciphertext: bytes) -> str:
        return self._cipher.decrypt(ciphertext[:12], ciphertext[12:], identity.encode()).decode()
