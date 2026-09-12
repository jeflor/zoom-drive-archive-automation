"""Zoom webhook authentication — the service's only defence against spoofing."""
import json

from auth import expected_signature, validation_token, verify_signature

SECRET = 'test-secret-token'
BODY = json.dumps({'event': 'recording.completed'}).encode()
TS = '1757721600'


def test_valid_signature_is_accepted():
    sig = expected_signature(SECRET, TS, BODY)
    assert verify_signature(SECRET, TS, BODY, sig)


def test_tampered_body_is_rejected():
    sig = expected_signature(SECRET, TS, BODY)
    assert not verify_signature(SECRET, TS, BODY + b' ', sig)


def test_replayed_timestamp_mismatch_is_rejected():
    sig = expected_signature(SECRET, TS, BODY)
    assert not verify_signature(SECRET, '1757721601', BODY, sig)


def test_wrong_secret_is_rejected():
    sig = expected_signature('other-secret', TS, BODY)
    assert not verify_signature(SECRET, TS, BODY, sig)


def test_missing_signature_is_rejected():
    assert not verify_signature(SECRET, TS, BODY, '')
    assert not verify_signature(SECRET, TS, BODY, None)


def test_signature_has_the_version_prefix_zoom_sends():
    assert expected_signature(SECRET, TS, BODY).startswith('v0=')


def test_signature_covers_raw_bytes_not_reparsed_json():
    """Re-serialising the body changes the bytes and must invalidate the sig."""
    raw = b'{"event":"recording.completed","a":1}'
    sig = expected_signature(SECRET, TS, raw)
    reserialised = json.dumps(json.loads(raw)).encode()
    assert reserialised != raw
    assert not verify_signature(SECRET, TS, reserialised, sig)


def test_url_validation_token_is_stable_and_secret_dependent():
    assert validation_token(SECRET, 'abc') == validation_token(SECRET, 'abc')
    assert validation_token(SECRET, 'abc') != validation_token('other', 'abc')
