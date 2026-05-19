import time

import jwt

from app import auth, config


def test_make_and_verify_jwt_roundtrip():
    token = auth.make_jwt(446619601)
    payload = auth.verify_jwt(token)
    assert payload is not None
    assert payload["account_id"] == 446619601
    assert payload["exp"] > int(time.time())


def test_verify_rejects_tampered_token():
    token = auth.make_jwt(123)
    # Flip a character in the signature segment
    head, body, sig = token.split(".")
    tampered = ".".join([head, body, "X" + sig[1:]])
    assert auth.verify_jwt(tampered) is None


def test_verify_rejects_wrong_secret():
    # Decode with the wrong secret should return None
    other_token = jwt.encode({"account_id": 123, "exp": int(time.time()) + 3600},
                             "different-secret", algorithm="HS256")
    assert auth.verify_jwt(other_token) is None


def test_verify_rejects_expired_token():
    payload = {"account_id": 123, "exp": int(time.time()) - 10}
    expired = jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
    assert auth.verify_jwt(expired) is None


def test_steamid64_conversion():
    # Known Steam ID 64 for account_id=446619601
    expected_steamid64 = 446619601 + 76561197960265728
    assert auth._steamid64_to_account_id(expected_steamid64) == 446619601
