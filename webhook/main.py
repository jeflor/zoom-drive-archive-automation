#!/usr/bin/env python3
"""
Zoom webhook receiver (Cloud Run service). On `recording.completed`, triggers
the zoom-autobackup Cloud Run JOB in single-meeting mode for that recording,
then returns 200 quickly so Zoom doesn't retry. Handles Zoom's endpoint URL
validation and verifies event signatures with the Secret Token.

Env: WEBHOOK_SECRET, GCP_PROJECT, REGION, JOB_NAME, PORT
"""
import os, json
from wsgiref.simple_server import make_server
import requests
import google.auth
from google.auth.transport.requests import Request as GReq

from auth import validation_token, verify_signature

SECRET = os.environ['WEBHOOK_SECRET']
PROJECT = os.environ.get('GCP_PROJECT', 'zoom-archive-demo')
REGION = os.environ.get('REGION', 'us-central1')
JOB = os.environ.get('JOB_NAME', 'zoom-autobackup')
PORT = int(os.environ.get('PORT', '8080'))

_creds = None
def gtoken():
    global _creds
    if _creds is None:
        _creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    if not _creds.valid:
        _creds.refresh(GReq())
    return _creds.token

def trigger_job(uuid):
    url = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run"
    body = {"overrides": {"containerOverrides": [{"env": [
        {"name": "MODE", "value": "single"},
        {"name": "MEETING_UUID", "value": uuid},
        {"name": "DRY_RUN", "value": "false"}]}]}}
    r = requests.post(url, headers={'Authorization': f'Bearer {gtoken()}', 'Content-Type': 'application/json'},
                      json=body, timeout=30)
    print(f"triggered job for {uuid[:16]}..: HTTP {r.status_code}", flush=True)
    return r.status_code

def app(environ, start):
    if environ.get('PATH_INFO') == '/healthz':
        start('200 OK', [('Content-Type', 'text/plain')]); return [b'ok']
    try:
        n = int(environ.get('CONTENT_LENGTH') or 0)
        raw = environ['wsgi.input'].read(n) if n else b''
        data = json.loads(raw or b'{}')
    except Exception:
        start('400 Bad Request', [('Content-Type', 'text/plain')]); return [b'bad']
    event = data.get('event')

    # 1) Zoom endpoint URL validation
    if event == 'endpoint.url_validation':
        pt = data['payload']['plainToken']
        out = json.dumps({'plainToken': pt,
                          'encryptedToken': validation_token(SECRET, pt)}).encode()
        start('200 OK', [('Content-Type', 'application/json')]); return [out]

    # 2) verify signature (v0:timestamp:body)
    ts = environ.get('HTTP_X_ZM_REQUEST_TIMESTAMP', '')
    sig = environ.get('HTTP_X_ZM_SIGNATURE', '')
    if not verify_signature(SECRET, ts, raw, sig):
        start('401 Unauthorized', [('Content-Type', 'text/plain')]); return [b'bad signature']

    # 3) act on recording.completed
    if event == 'recording.completed':
        uuid = data.get('payload', {}).get('object', {}).get('uuid')
        if uuid:
            try: trigger_job(uuid)
            except Exception as e: print(f"trigger error: {e}", flush=True)
    start('200 OK', [('Content-Type', 'text/plain')]); return [b'ok']

if __name__ == '__main__':
    make_server('', PORT, app).serve_forever()
