"""Encrypt recoverable API-token material with the gateway's existing key."""
from cryptography.fernet import Fernet, InvalidToken


def encrypt_token_secret(config, secret: str) -> str:
    return Fernet(config.encryption_key).encrypt(secret.encode()).decode()


def decrypt_token_secret(config, ciphertext: str) -> str | None:
    try:
        return Fernet(config.encryption_key).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
        return None
