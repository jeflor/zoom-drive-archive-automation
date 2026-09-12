"""Zoom webhook authentication.

Two separate mechanisms, both keyed on the app's Secret Token:

1. Endpoint URL validation — Zoom posts a `plainToken` when you save the
   subscription and expects the HMAC back, proving you hold the secret.
2. Event signatures — every real event carries `x-zm-signature` over
   `v0:{timestamp}:{raw body}`. The signature is computed over the *raw*
   bytes, so it must be checked before any re-serialisation of the JSON.

Kept in its own module (rather than inline in main.py) so the signature check
is unit-testable without standing up the service.
"""
import hashlib
import hmac


def validation_token(secret: str, plain_token: str) -> str:
    """The `encryptedToken` Zoom expects back during URL validation."""
    return hmac.new(secret.encode(), plain_token.encode(),
                    hashlib.sha256).hexdigest()


def expected_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Signature Zoom should have sent for this exact request body."""
    message = b'v0:' + timestamp.encode() + b':' + raw_body
    return 'v0=' + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str, raw_body: bytes,
                     received_signature: str) -> bool:
    """Constant-time check of an inbound Zoom event signature."""
    if not received_signature:
        return False
    return hmac.compare_digest(
        expected_signature(secret, timestamp, raw_body), received_signature)
