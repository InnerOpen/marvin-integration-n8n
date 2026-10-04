"""How a request to an n8n Webhook node proves it came from Marvin — no dependencies.

Every mode signs (or carries) the exact bytes that are sent, so a body re-serialised on the way can
never verify by accident:

- ``jwt`` (default): an HS256 JWT in ``Authorization: Bearer …``. n8n's own *JWT Auth* credential on
  the Webhook node checks it, so nothing is coded in n8n. It lives 60 seconds and carries the SHA-256
  of the body, so a token copied out of n8n's execution data (which keeps request headers) is useless.
- ``hmac``: ``X-Marvin-Signature: t=<ts>,v1=<hex HMAC-SHA256 of "{t}.{body}">`` for a workflow that
  verifies it itself (n8n has no native HMAC check).
- ``header``: ``<auth_header>: <secret>`` — the header *is* the secret, kept for existing
  ``X-Marvin-Hook`` setups.
- ``none``: nothing.

Nothing here logs, and the returned headers are for the request only: callers never put them in a
result, an error or a log line.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

AUTH_JWT, AUTH_HEADER, AUTH_HMAC, AUTH_NONE = "jwt", "header", "hmac", "none"
AUTH_MODES = (AUTH_JWT, AUTH_HEADER, AUTH_HMAC, AUTH_NONE)

JWT_ISSUER = "marvin"
JWT_TTL_SECONDS = 60
SIGNATURE_HEADER = "X-Marvin-Signature"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _compact(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def jwt_hs256(claims: dict, secret: str) -> str:
    """A compact HS256 JWT (RFC 7519) over ``claims``."""
    signing_input = f"{_b64url(_compact({'alg': 'HS256', 'typ': 'JWT'}))}.{_b64url(_compact(claims))}"
    signature = hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def webhook_claims(*, path: str, body: bytes, issued_at: int, jti: str) -> dict:
    """The claims Marvin signs: who (``iss``), which webhook (``aud``), when (``iat``/``exp``), which
    attempt (``jti`` = the idempotency key) and which bytes (``body_sha256``)."""
    return {
        "iss": JWT_ISSUER,
        "aud": path,
        "iat": issued_at,
        "exp": issued_at + JWT_TTL_SECONDS,
        "jti": jti,
        "body_sha256": body_sha256(body),
    }


def hmac_signature(body: bytes, secret: str, timestamp: int) -> str:
    """``t=<ts>,v1=<hex>``: HMAC-SHA256 of ``"{t}.{body}"``, keyed with the secret."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def auth_headers(mode: str, *, secret: str, body: bytes, path: str, issued_at: int, jti: str, header_name: str) -> dict[str, str]:
    """The request headers that authenticate ``body`` under ``mode``."""
    if mode == AUTH_JWT:
        return {"Authorization": f"Bearer {jwt_hs256(webhook_claims(path=path, body=body, issued_at=issued_at, jti=jti), secret)}"}
    if mode == AUTH_HMAC:
        return {SIGNATURE_HEADER: hmac_signature(body, secret, issued_at)}
    if mode == AUTH_HEADER:
        return {header_name: secret}
    if mode == AUTH_NONE:
        return {}
    raise ValueError(f"auth must be one of {', '.join(AUTH_MODES)}, not {mode!r}.")
