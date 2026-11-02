"""AES-256-GCM envelope encryption and key fingerprinting for stored credentials."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_SIZE_BYTES = 12
DEK_SIZE_BYTES = 32


@dataclass(frozen=True, slots=True)
class EncryptedEnvelope:
    """Result of encrypting a secret with a per-record DEK wrapped by the master KEK."""

    encrypted_secret: bytes
    encrypted_dek: bytes
    key_fingerprint: str
    masked_preview: str
    key_version: int = 1


def compute_key_fingerprint(secret: str) -> tuple[str, str]:
    """Computes a SHA-256 fingerprint and a safe masked preview (never reversible)."""
    cleaned = secret.strip()
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    fingerprint = f"sha256:{digest[:24]}"
    if len(cleaned) <= 8:
        masked = "****"
    else:
        prefix = cleaned[:6] if cleaned.startswith(("sk-", "AIza", "gsk_", "xi_")) else cleaned[:3]
        suffix = cleaned[-4:]
        masked = f"{prefix}...{suffix}"
    return fingerprint, masked


class EnvelopeEncryptor:
    """Performs two-tier AES-256-GCM envelope encryption for upstream API credentials."""

    def __init__(self, master_kek: bytes, key_version: int = 1) -> None:
        if len(master_kek) != 32:
            raise ValueError(f"Master KEK must be exactly 32 bytes, got {len(master_kek)}")
        self._kek_aesgcm = AESGCM(master_kek)
        self._key_version = key_version

    def encrypt(
        self,
        plaintext_secret: str,
        *,
        provider_id: str,
        credential_id: str,
    ) -> EncryptedEnvelope:
        """Encrypts plaintext_secret with a fresh 256-bit DEK and wraps the DEK with the master KEK."""
        aad = f"{provider_id}:{credential_id}".encode("utf-8")

        # 1. Generate unique 256-bit DEK
        dek = os.urandom(DEK_SIZE_BYTES)
        dek_aesgcm = AESGCM(dek)

        # 2. Encrypt secret with DEK
        secret_nonce = os.urandom(NONCE_SIZE_BYTES)
        secret_ct = dek_aesgcm.encrypt(
            secret_nonce, plaintext_secret.encode("utf-8"), aad
        )
        encrypted_secret = secret_nonce + secret_ct

        # 3. Wrap DEK with Master KEK
        dek_nonce = os.urandom(NONCE_SIZE_BYTES)
        wrapped_dek = self._kek_aesgcm.encrypt(dek_nonce, dek, aad)
        encrypted_dek = dek_nonce + wrapped_dek

        fingerprint, masked = compute_key_fingerprint(plaintext_secret)
        return EncryptedEnvelope(
            encrypted_secret=encrypted_secret,
            encrypted_dek=encrypted_dek,
            key_fingerprint=fingerprint,
            masked_preview=masked,
            key_version=self._key_version,
        )

    def decrypt(
        self,
        encrypted_secret: bytes,
        encrypted_dek: bytes,
        *,
        provider_id: str,
        credential_id: str,
    ) -> str:
        """Unwraps the DEK using the master KEK and decrypts the secret in memory."""
        aad = f"{provider_id}:{credential_id}".encode("utf-8")

        dek_nonce = encrypted_dek[:NONCE_SIZE_BYTES]
        wrapped_dek = encrypted_dek[NONCE_SIZE_BYTES:]
        dek = self._kek_aesgcm.decrypt(dek_nonce, wrapped_dek, aad)

        secret_nonce = encrypted_secret[:NONCE_SIZE_BYTES]
        secret_ct = encrypted_secret[NONCE_SIZE_BYTES:]
        dek_aesgcm = AESGCM(dek)
        plaintext_bytes = dek_aesgcm.decrypt(secret_nonce, secret_ct, aad)
        return plaintext_bytes.decode("utf-8")

