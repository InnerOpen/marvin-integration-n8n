"""The hand-rolled signers, checked against an independent implementation (PyJWT, dev-only)."""

import hashlib
import hmac
import re
import time

import jwt
import pytest

from marvin_integration_n8n.auth import (
    JWT_TTL_SECONDS,
    SIGNATURE_HEADER,
    auth_headers,
    hmac_signature,
    jwt_hs256,
    webhook_claims,
)

SECRET = "a-long-n8n-jwt-passphrase-of-32-bytes+"
BODY = b'{"marvin":{"entry_id":"e1"},"data":{"x":1}}'
PATH = "marvin/new-entry"


def _token(issued_at=None, body=BODY, path=PATH, jti="idem-1"):
    claims = webhook_claims(path=path, body=body, issued_at=int(time.time()) if issued_at is None else issued_at, jti=jti)
    return jwt_hs256(claims, SECRET)


def test_jwt_verifies_with_pyjwt_and_carries_the_documented_claims():
    now = int(time.time())
    claims = jwt.decode(_token(now), SECRET, algorithms=["HS256"], audience=PATH, issuer="marvin")
    assert claims == {
        "iss": "marvin",
        "aud": PATH,
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
        "jti": "idem-1",
        "body_sha256": hashlib.sha256(BODY).hexdigest(),
    }
    assert jwt.get_unverified_header(_token(now)) == {"alg": "HS256", "typ": "JWT"}


def test_jwt_lives_sixty_seconds():
    assert JWT_TTL_SECONDS == 60
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(_token(int(time.time()) - 61), SECRET, algorithms=["HS256"], audience=PATH)


def test_jwt_is_bound_to_the_path_and_the_secret():
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(_token(), SECRET, algorithms=["HS256"], audience="marvin/other")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(_token(), "another-passphrase-entirely-32-bytes-long", algorithms=["HS256"], audience=PATH)


def test_jwt_body_hash_changes_with_one_byte():
    a = jwt.decode(_token(body=BODY), SECRET, algorithms=["HS256"], audience=PATH)
    b = jwt.decode(_token(body=BODY.replace(b"1", b"2")), SECRET, algorithms=["HS256"], audience=PATH)
    assert a["body_sha256"] != b["body_sha256"]


def test_hmac_signature_format_and_value():
    sig = hmac_signature(BODY, SECRET, 1_760_000_000)
    assert re.fullmatch(r"t=1760000000,v1=[0-9a-f]{64}", sig)
    expected = hmac.new(SECRET.encode(), b"1760000000." + BODY, hashlib.sha256).hexdigest()
    assert sig.endswith(f"v1={expected}")


def test_auth_headers_per_mode():
    common = {"secret": SECRET, "body": BODY, "path": PATH, "issued_at": 1_760_000_000, "jti": "k", "header_name": "X-Marvin-Hook"}
    (bearer,) = auth_headers("jwt", **common).items()
    assert bearer[0] == "Authorization" and bearer[1].startswith("Bearer ")
    assert auth_headers("hmac", **common) == {SIGNATURE_HEADER: hmac_signature(BODY, SECRET, 1_760_000_000)}
    assert auth_headers("header", **common) == {"X-Marvin-Hook": SECRET}
    assert auth_headers("none", **common) == {}
    with pytest.raises(ValueError):
        auth_headers("basic", **common)
