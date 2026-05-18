import os

import pytest

from app import crypto


def test_encrypt_decrypt_roundtrip():
    plain = "sk-ant-api03-fake-key-for-test-only"
    cipher = crypto.encrypt_key(plain)
    assert cipher != plain
    assert crypto.decrypt_key(cipher) == plain


def test_decrypt_with_wrong_key_raises():
    """Tampered ciphertext should be rejected via SystemExit (we raise to fail loud)."""
    cipher = crypto.encrypt_key("hello")
    # Mutate one character in the middle to invalidate the token
    bad = cipher[:-5] + "ZZZZ" + cipher[-1]
    with pytest.raises(SystemExit):
        crypto.decrypt_key(bad)


def test_missing_fernet_key_raises():
    saved = os.environ.pop("FERNET_KEY")
    try:
        with pytest.raises(SystemExit):
            crypto.encrypt_key("anything")
    finally:
        os.environ["FERNET_KEY"] = saved


def test_generate_key_format():
    k = crypto.generate_key()
    # Fernet keys are 44 chars (32 bytes url-safe-base64, padded)
    assert len(k) == 44
    assert k.endswith("=")
