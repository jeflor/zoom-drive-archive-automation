#!/usr/bin/env python3
"""
Cloud Run Job: back up Zoom cloud recordings to Google Drive.

Streams each recording straight from Zoom into Drive using Drive's resumable
upload protocol, in chunks. Nothing is buffered to disk or held whole in
memory, so a 6.6 GB recording costs ~8 MB of RAM rather than 6.6 GB. This
matters on Cloud Run, where /tmp is RAM-backed.

No state bucket is required. The job is idempotent because it lists what is
already in each target Drive folder and skips those names, so re-running only
transfers what is missing.

Sharding: if CLOUD_RUN_TASK_COUNT > 1, each task takes an interleaved slice of
the work list, so N tasks run in parallel with no coordination.

Environment:
    ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET
    DRIVE_ROOT_FOLDER_ID        zBackup root folder id
    DRIVE_TOKEN_JSON            path to a token.json (client_id/secret/refresh_token)
    WORKLIST                    path to backup_worklist.json (baked into image)
    WORKERS                     concurrent transfers per task (default 8)
    DRY_RUN                     "true" to log without transferring
"""

import os
import re
import sys
import json
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

ZOOM_ACCOUNT_ID = os.environ['ZOOM_ACCOUNT_ID']
ZOOM_CLIENT_ID = os.environ['ZOOM_CLIENT_ID']
ZOOM_CLIENT_SECRET = os.environ['ZOOM_CLIENT_SECRET']
DRIVE_ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
TOKEN_PATH = os.environ.get('DRIVE_TOKEN_JSON', '/secrets/token.json')
WORKLIST = os.environ.get('WORKLIST', 'backup_worklist.json')
WORKERS = int(os.environ.get('WORKERS', '8'))
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'

TASK_INDEX = int(os.environ.get('CLOUD_RUN_TASK_INDEX', '0'))
TASK_COUNT = int(os.environ.get('CLOUD_RUN_TASK_COUNT', '1'))

CHUNK = 8 * 1024 * 1024          # must be a multiple of 256 KB
MEDIA = {'MP4', 'M4A'}
EXT = {'MP4': 'mp4', 'M4A': 'm4a', 'CHAT': 'txt', 'TRANSCRIPT': 'vtt',
       'TIMELINE': 'json', 'CC': 'vtt', 'SUMMARY': 'json', 'CSV': 'csv'}

lock = threading.Lock()
stats = {'files': 0, 'bytes': 0, 'skip': 0, 'fail': 0}


# ---------- auth ----------

_zoom = {'tok': None, 'at': 0}


def zoom_token():
    with lock:
        if _zoom['tok'] and time.time() - _zoom['at'] < 2400:
            return _zoom['tok']
        b = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
        r = requests.post('https://zoom.us/oauth/token',
                          params={'grant_type': 'account_credentials',
                                  'account_id': ZOOM_ACCOUNT_ID},
                          headers={'Authorization': f'Basic {b}'}, timeout=60)
        r.raise_for_status()
        _zoom['tok'] = r.json()['access_token']
        _zoom['at'] = time.time()
        return _zoom['tok']


_creds = Credentials.from_authorized_user_file(
    TOKEN_PATH, ['https://www.googleapis.com/auth/drive'])


def drive_token():
    with lock:
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token


def dheaders():
    return {'Authorization': f'Bearer {drive_token()}'}


# ---------- drive helpers (raw HTTP; no client object to keep threads simple) ----------

DRIVE_API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3/files'
COMMON = {'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
          'corpora': 'allDrives'}

_folders = {}


def get_folder(name, parent):
    key = (parent, name)
    with lock:
        if key in _folders:
            return _folders[key]
    esc = name.replace("\\", "\\\\").replace("'", "\\'")
    q = (f"name='{esc}' and '{parent}' in parents "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    r = requests.get(DRIVE_API, headers=dheaders(),
                     params={**COMMON, 'q': q, 'fields': 'files(id)'}, timeout=60)
    r.raise_for_status()
    hits = r.json().get('files', [])
    if hits:
        fid = hits[0]['id']
    else:
        r = requests.post(DRIVE_API, headers={**dheaders(), 'Content-Type': 'application/json'},
                          params={'supportsAllDrives': 'true', 'fields': 'id'},
                          json={'name': name, 'parents': [parent],
                                'mimeType': 'application/vnd.google-apps.folder'}, timeout=60)
        r.raise_for_status()
        fid = r.json()['id']
    with lock:
        _folders[key] = fid
    return fid


def folder_contents(folder):
    """Names already present -> lets a re-run skip finished work."""
    names, page = set(), None
    while True:
        p = {**COMMON, 'q': f"'{folder}' in parents and trashed=false",
             'fields': 'nextPageToken, files(name)', 'pageSize': '1000'}
        if page:
            p['pageToken'] = page
        r = requests.get(DRIVE_API, headers=dheaders(), params=p, timeout=60)
        r.raise_for_status()
        d = r.json()
        names |= {f['name'] for f in d.get('files', [])}
        page = d.get('nextPageToken')
        if not page:
            return names


def stream_to_drive(download_url, name, folder, total):
    """Zoom -> Drive with no disk and no whole-file buffering.

    Opens a Drive resumable session, then pumps fixed-size chunks out of the
    Zoom response as they arrive. Returns the size Drive reports.
    """
    r = requests.post(UPLOAD_API,
                      headers={**dheaders(), 'Content-Type': 'application/json; charset=UTF-8'},
                      params={'uploadType': 'resumable', 'supportsAllDrives': 'true'},
                      json={'name': name, 'parents': [folder]}, timeout=60)
    r.raise_for_status()
    session = r.headers['Location']

    sent = 0
    buf = b''
    with requests.get(download_url, headers={'Authorization': f'Bearer {zoom_token()}'},
                      stream=True, timeout=900) as resp:
        resp.raise_for_status()
        for piece in resp.iter_content(chunk_size=CHUNK):
            if not piece:
                continue
            buf += piece
            # send whole chunks; keep the tail for the final request
            while len(buf) >= CHUNK:
                block, buf = buf[:CHUNK], buf[CHUNK:]
                last = (sent + len(block) >= total) if total else False
                end = sent + len(block) - 1
                rng = f'bytes {sent}-{end}/{total if total else "*"}'
                pr = requests.put(session, headers={'Content-Range': rng}, data=block, timeout=600)
                if pr.status_code not in (200, 201, 308):
                    raise RuntimeError(f'chunk {rng} -> HTTP {pr.status_code} {pr.text[:120]}')
                sent += len(block)
        # final chunk (may be empty only if total was an exact multiple)
        end = sent + len(buf) - 1
        rng = f'bytes {sent}-{end}/{sent + len(buf)}'
        pr = requests.put(session, headers={'Content-Range': rng}, data=buf, timeout=600)
        if pr.status_code not in (200, 201):
            raise RuntimeError(f'final {rng} -> HTTP {pr.status_code} {pr.text[:160]}')
        fid = pr.json().get('id')
        # The resumable-upload response omits 'size', so read the authoritative
        # size back by id. This is also the post-upload verification: it proves
        # the bytes are actually in Drive, not just that the PUT returned 200.
        m = requests.get(f'{DRIVE_API}/{fid}', headers=dheaders(),
                         params={'fields': 'size', 'supportsAllDrives': 'true'}, timeout=60)
        m.raise_for_status()
        return int(m.json().get('size', 0)), fid


def delete_file(fid):
    requests.delete(f'{DRIVE_API}/{fid}', headers=dheaders(),
                    params={'supportsAllDrives': 'true'}, timeout=60)


# ---------- work ----------

def sanitize(s):
    return re.sub(r'[\\/:*?"<>|]', '_', s)[:120]


def handle_file(f, folder, ts, topic, present):
    ftype = f.get('file_type')
    ext = (f.get('file_extension') or EXT.get(ftype, 'bin')).lower().lstrip('.')
    name = sanitize(f"{ts} {topic} ({f.get('recording_type', ftype)})") + f'.{ext}'
    if name in present:
        with lock:
            stats['skip'] += 1
        return
    size = f.get('file_size', 0)
    if DRY_RUN:
        with lock:
            stats['files'] += 1
            stats['bytes'] += size
            print(f'  [dry-run] {name} ({size/1e6:.0f} MB)', flush=True)
        return
    # Media gets many retries with hard backoff (worth waiting out Drive's 403
    # rate-limiting). Metadata (timeline/summary/cc/chat) fails fast so it never
    # stalls the run — it's tiny and non-essential.
    tries = 7 if ftype in MEDIA else 2
    for attempt in range(tries):
        try:
            got, fid = stream_to_drive(f['download_url'], name, folder, size)
            if ftype in MEDIA and size and got != size:
                delete_file(fid)
                raise RuntimeError(f'size mismatch zoom={size} drive={got}')
            with lock:
                stats['files'] += 1
                stats['bytes'] += got
                print(f'  ok {name[:70]} {got/1e6:.0f} MB', flush=True)
            return
        except Exception as e:
            if attempt == tries - 1:
                with lock:
                    stats['fail'] += 1
                    print(f'  FAIL {name[:60]}: {str(e)[:120]}', flush=True)
            elif ftype in MEDIA:
                wait = min(90, 4 * (2 ** attempt)) + (hash(name) % 5)  # ride out 403 rate-limits
                time.sleep(wait)
            else:
                time.sleep(2)


def main():
    work = json.load(open(WORKLIST))
    mine = [w for i, w in enumerate(work) if i % TASK_COUNT == TASK_INDEX]
    print(f'task {TASK_INDEX+1}/{TASK_COUNT}: {len(mine)} of {len(work)} meetings, '
          f'{WORKERS} workers, dry_run={DRY_RUN}', flush=True)

    users = {u['email']: u['id'] for u in requests.get(
        'https://api.zoom.us/v2/users', headers={'Authorization': f'Bearer {zoom_token()}'},
        params={'page_size': 300, 'status': 'active'}, timeout=60).json().get('users', [])}

    t0 = time.time()
    for n, w in enumerate(mine, 1):
        uid = users.get(w['host'])
        if not uid:
            continue
        rr = requests.get(f'https://api.zoom.us/v2/users/{uid}/recordings',
                          headers={'Authorization': f'Bearer {zoom_token()}'},
                          params={'from': w['date'], 'to': w['date'], 'page_size': 300}, timeout=60)
        if rr.status_code != 200:
            print(f'  list {w["date"]} -> HTTP {rr.status_code}', flush=True)
            continue
        mt = next((m for m in rr.json().get('meetings', []) if m['uuid'] == w['uuid']), None)
        if not mt:
            continue
        ts = (w['start_time'] or '').replace('T', ' ').replace('Z', '')
        hf = get_folder(sanitize(w['host']), DRIVE_ROOT)
        mf = get_folder(sanitize(f"{ts}: {w['topic']}"), hf)
        present = folder_contents(mf)
        files = [f for f in mt.get('recording_files', []) if f.get('status') == 'completed']
        print(f'[{n}/{len(mine)}] {w["date"]} {w["topic"][:50]}', flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(lambda f: handle_file(f, mf, ts, w['topic'], present), files))
        if n % 25 == 0:
            el = time.time() - t0
            print(f'  ...{stats["files"]} files, {stats["bytes"]/1e9:.1f} GB, '
                  f'{stats["bytes"]/1e6/max(el,1):.1f} MB/s', flush=True)

    el = time.time() - t0
    print(f'\nDONE task {TASK_INDEX+1}/{TASK_COUNT}: files={stats["files"]} '
          f'skip={stats["skip"]} fail={stats["fail"]} {stats["bytes"]/1e9:.2f} GB '
          f'in {el/60:.1f} min ({stats["bytes"]/1e6/max(el,1):.1f} MB/s)', flush=True)
    sys.exit(1 if stats['fail'] else 0)


if __name__ == '__main__':
    main()
