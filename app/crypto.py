"""Symmetric encryption for BYO Anthropic API keys stored in the DB.

Anyone with the DB and FERNET_KEY can decrypt. This is acceptable for a self-hosted
beta — see docs/SECURITY.md for the threat model.
"""

import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.getenv("FERNET_KEY", "").encode()
    if not key:
        raise SystemExit(
            "FERNET_KEY is not set in .env — generate one with "
            "`docker compose run --rm app python -m app.crypto gen`"
        )
    return Fernet(key)


def encrypt_key(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise SystemExit("Stored key cannot be decrypted with current FERNET_KEY") from e


def generate_key() -> str:
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "gen":
        print(generate_key())
    else:
        print("usage: python -m app.crypto gen", file=sys.stderr)
        sys.exit(1)
