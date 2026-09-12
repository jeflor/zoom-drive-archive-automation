#!/usr/bin/env python3
"""
Back up + trash per-participant "separate audio" files that only appear via the
by-UUID recording detail endpoint (needs cloud_recording:read:list_recording_files:admin).

Option-A style: for each target meeting, match participant-audio files to the
meeting's Drive folder by EXACT byte size; upload any missing; then trash (soft)
only the ones whose bytes are confirmed present in Drive. NON-destructive unless
--trash is passed.

Usage:
    ./venv/bin/python scripts/participant_audio_backfill.py            # report + upload missing, NO trash
    ./venv/bin/python scripts/participant_audio_backfill.py --trash    # also trash verified files
"""
import base64, requests, re, sys, os, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import CONFIG
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

TOKEN = os.path.join(os.path.dirname(__file__), '..', 'token.json')
ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
DRIVE_API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3/files'
COMMON = {'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true', 'corpora': 'allDrives'}
FOLDER_MIME = 'application/vnd.google-apps.folder'
CHUNK = 8 * 1024 * 1024
DO_TRASH = '--trash' in sys.argv

# (host, start_time) for the 3 meetings with participant audio (Aug 2026 gap)
TARGETS = [
    ('archive@example.com', '2026-08-13T18:52:29Z'),
    ('archive@example.com', '2026-08-06T19:01:41Z'),
    ('archive@example.com', '2026-08-05T13:21:54Z'),
]


def sanitize(s): return re.sub(r'[\\/:*?"<>|]', '_', s)[:120]
def enc(u):
    return urllib.parse.quote(urllib.parse.quote(u, safe=''), safe='') if (u.startswith('/') or '//' in u) else urllib.parse.quote(u, safe='')


def ztok():
    b = base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()
    r = requests.post('https://zoom.us/oauth/token',
                      params={'grant_type': 'account_credentials', 'account_id': CONFIG['ZOOM_ACCOUNT_ID']},
                      headers={'Authorization': f'Basic {b}'}, timeout=60)
    r.raise_for_status(); return r.json()['access_token']


creds = Credentials.from_authorized_user_file(TOKEN, ['https://www.googleapis.com/auth/drive'])
def dtok():
    if not creds.valid: creds.refresh(Request())
    return creds.token
def dh(): return {'Authorization': f'Bearer {dtok()}'}
ZT = ztok(); ZH = {'Authorization': f'Bearer {ZT}'}


def dlist(params):
    p = {**COMMON, 'pageSize': '1000', **params}
    out = []; page = None
    while True:
        if page: p['pageToken'] = page
        d = requests.get(DRIVE_API, headers=dh(), params=p, timeout=60).json()
        out += d.get('files', []); page = d.get('nextPageToken')
        if not page: return out


def find_folder(name):
    esc = name.replace('\\', '\\\\').replace("'", "\\'")
    fs = dlist({'q': f"name='{esc}' and mimeType='{FOLDER_MIME}' and trashed=false",
                'fields': 'files(id,name,parents)'})
    return fs


def meeting(host, start):
    d = requests.get(f'https://api.zoom.us/v2/users/{host}/recordings', headers=ZH,
                     params={'from': start[:10], 'to': start[:10], 'page_size': 300}, timeout=60).json()
    return next(m for m in d.get('meetings', []) if m.get('start_time') == start)


def stream(url, name, parent):
    r = requests.post(UPLOAD_API, headers={**dh(), 'Content-Type': 'application/json; charset=UTF-8'},
                      params={'uploadType': 'resumable', 'supportsAllDrives': 'true'},
                      json={'name': name, 'parents': [parent]}, timeout=60)
    r.raise_for_status(); sess = r.headers['Location']; sent = 0; buf = b''
    with requests.get(url, headers={'Authorization': f'Bearer {ZT}'}, stream=True, timeout=1800) as resp:
        resp.raise_for_status()
        for piece in resp.iter_content(chunk_size=CHUNK):
            if not piece: continue
            buf += piece
            while len(buf) >= CHUNK:
                block, buf = buf[:CHUNK], buf[CHUNK:]
                rng = f'bytes {sent}-{sent+len(block)-1}/*'
                pr = requests.put(sess, headers={'Content-Range': rng}, data=block, timeout=600)
                if pr.status_code not in (200, 201, 308): raise RuntimeError(f'{rng} {pr.status_code}')
                sent += len(block)
        rng = f'bytes {sent}-{sent+len(buf)-1}/{sent+len(buf)}'
        pr = requests.put(sess, headers={'Content-Range': rng}, data=buf, timeout=600)
        if pr.status_code not in (200, 201): raise RuntimeError(f'final {pr.status_code}')
        return int(requests.get(f'{DRIVE_API}/{pr.json()["id"]}', headers=dh(),
                                params={'fields': 'size', 'supportsAllDrives': 'true'}, timeout=60).json().get('size', 0))


print(f"MODE: {'BACKUP + TRASH' if DO_TRASH else 'BACKUP-ONLY (no trash)'}\n")
tot_up = tot_trash = tot_skip = 0
for host, start in TARGETS:
    m = meeting(host, start)
    ts = start.replace('T', ' ').replace('Z', ''); topic = m.get('topic', '')
    folder_name = sanitize(f"{ts}: {topic}")
    folders = find_folder(folder_name)
    print(f"== {topic} | {start} ==")
    if not folders:
        print(f"   !! Drive folder not found ({folder_name!r}) — SKIP all"); continue
    fid = folders[0]['id']
    if len(folders) > 1:
        print(f"   (note: {len(folders)} folders match this name; using {fid})")
    dj = requests.get(f"https://api.zoom.us/v2/meetings/{enc(m['uuid'])}/recordings", headers=ZH, timeout=60).json()
    pa = dj.get('participant_audio_files', []) or []
    present = {int(x['size']) for x in dlist({'q': f"'{fid}' in parents and trashed=false", 'fields': 'files(size)'}) if x.get('size')}
    for f in pa:
        sz = f.get('file_size'); rt = f.get('recording_type') or f.get('file_name') or 'audio_only'
        ext = (f.get('file_extension') or 'M4A').lower().lstrip('.')
        if sz not in present:
            name = sanitize(f"{ts} {topic} ({rt})") + f'.{ext}'
            got = stream(f['download_url'], name, fid)
            ok = (got == sz)
            print(f"   {'UP' if ok else 'UP?'} {rt:38} {sz/1e6:6.1f}MB -> {name} ({got}b)")
            if ok: present.add(sz)
            tot_up += 1
        else:
            print(f"   OK (already in Drive) {rt:34} {sz/1e6:6.1f}MB")
    # trash phase — gated on byte presence
    if DO_TRASH:
        present = {int(x['size']) for x in dlist({'q': f"'{fid}' in parents and trashed=false", 'fields': 'files(size)'}) if x.get('size')}
        for f in pa:
            sz = f.get('file_size'); rt = f.get('recording_type')
            if sz not in present:
                print(f"   SKIP trash (unverified) {rt} {sz}"); tot_skip += 1; continue
            r = requests.delete(f"https://api.zoom.us/v2/meetings/{enc(m['uuid'])}/recordings/{f['id']}",
                                params={'action': 'trash'}, headers=ZH, timeout=60)
            if r.status_code in (200, 204): print(f"   TRASHED {rt} {sz/1e6:.1f}MB"); tot_trash += 1
            else: print(f"   TRASH FAIL {rt} {r.status_code} {r.text[:100]}"); tot_skip += 1
    print()
print(f"uploaded={tot_up} trashed={tot_trash} skipped={tot_skip}")
