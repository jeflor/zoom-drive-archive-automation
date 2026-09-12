#!/usr/bin/env python3
"""
Phase-2 auto backup + delete. Cloud Run JOB with two modes:
  MODE=sweep   : back up + delete every recording in the last DAYS days
  MODE=single  : back up + delete one meeting (MEETING_UUID)

Per meeting: stream every recording file into Drive (chunked, no disk buffer),
verify each MEDIA file's bytes landed exactly, then delete the media (MP4/M4A)
from Zoom — keeping the transcript. Deletion is gated on Drive verification.

Env: ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET, DRIVE_ROOT_FOLDER_ID,
     DRIVE_TOKEN_JSON, MODE, MEETING_UUID, DAYS(=7), DRY_RUN
"""
import os, re, json, time, base64, urllib.parse, requests
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

ZA, ZC, ZS = os.environ['ZOOM_ACCOUNT_ID'], os.environ['ZOOM_CLIENT_ID'], os.environ['ZOOM_CLIENT_SECRET']
ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
TOKEN_PATH = os.environ.get('DRIVE_TOKEN_JSON', '/secrets/token.json')
MODE = os.environ.get('MODE', 'sweep')
MEETING_UUID = os.environ.get('MEETING_UUID', '')
DAYS = int(os.environ.get('DAYS', '7'))
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
MEDIA = {'MP4', 'M4A'}
CHUNK = 8 * 1024 * 1024
DRIVE_API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3/files'
COMMON = {'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true', 'corpora': 'allDrives'}
EXT = {'MP4': 'mp4', 'M4A': 'm4a', 'CHAT': 'txt', 'TRANSCRIPT': 'vtt', 'TIMELINE': 'json', 'CC': 'vtt', 'SUMMARY': 'json', 'CSV': 'csv'}

_zt = {'v': None, 't': 0}
def ztok():
    if _zt['v'] and time.time() - _zt['t'] < 2400:
        return _zt['v']
    b = base64.b64encode(f"{ZC}:{ZS}".encode()).decode()
    r = requests.post('https://zoom.us/oauth/token', params={'grant_type': 'account_credentials', 'account_id': ZA},
                      headers={'Authorization': f'Basic {b}'}, timeout=60)
    r.raise_for_status(); _zt['v'] = r.json()['access_token']; _zt['t'] = time.time(); return _zt['v']

_creds = Credentials.from_authorized_user_file(TOKEN_PATH, ['https://www.googleapis.com/auth/drive'])
def dtok():
    if not _creds.valid: _creds.refresh(Request())
    return _creds.token
def dh(): return {'Authorization': f'Bearer {dtok()}'}
def enc(u): return urllib.parse.quote(urllib.parse.quote(u, safe=''), safe='') if (u.startswith('/') or '//' in u) else urllib.parse.quote(u, safe='')
def sanitize(s): return re.sub(r'[\\/:*?"<>|]', '_', s)[:120]

_fc = {}
def folder(name, parent):
    k = (parent, name)
    if k in _fc: return _fc[k]
    q = f"name='{name.replace(chr(39), chr(92)+chr(39))}' and '{parent}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = requests.get(DRIVE_API, headers=dh(), params={**COMMON, 'q': q, 'fields': 'files(id)'}, timeout=60).json().get('files', [])
    fid = r[0]['id'] if r else requests.post(DRIVE_API, headers={**dh(), 'Content-Type': 'application/json'},
        params={'supportsAllDrives': 'true', 'fields': 'id'},
        json={'name': name, 'parents': [parent], 'mimeType': 'application/vnd.google-apps.folder'}, timeout=60).json()['id']
    _fc[k] = fid; return fid

def folder_names(fid):
    names, page = set(), None
    while True:
        p = {**COMMON, 'q': f"'{fid}' in parents and trashed=false", 'fields': 'nextPageToken, files(name)', 'pageSize': '1000'}
        if page: p['pageToken'] = page
        d = requests.get(DRIVE_API, headers=dh(), params=p, timeout=60).json()
        names |= {f['name'] for f in d.get('files', [])}
        page = d.get('nextPageToken')
        if not page: return names

def folder_files(fid):
    """(name, size) of everything in a folder — for size-based gap detection."""
    out, page = [], None
    while True:
        p = {**COMMON, 'q': f"'{fid}' in parents and trashed=false", 'fields': 'nextPageToken, files(name,size)', 'pageSize': '1000'}
        if page: p['pageToken'] = page
        d = requests.get(DRIVE_API, headers=dh(), params=p, timeout=60).json()
        out += [(f['name'], int(f.get('size', 0))) for f in d.get('files', [])]
        page = d.get('nextPageToken')
        if not page: return out

def stream_to_drive(url, name, parent):
    r = requests.post(UPLOAD_API, headers={**dh(), 'Content-Type': 'application/json; charset=UTF-8'},
                      params={'uploadType': 'resumable', 'supportsAllDrives': 'true'},
                      json={'name': name, 'parents': [parent]}, timeout=60)
    r.raise_for_status(); sess = r.headers['Location']
    sent = 0; buf = b''
    with requests.get(url, headers={'Authorization': f'Bearer {ztok()}'}, stream=True, timeout=1800) as resp:
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
        fid = pr.json().get('id')
        m = requests.get(f'{DRIVE_API}/{fid}', headers=dh(), params={'fields': 'size', 'supportsAllDrives': 'true'}, timeout=60)
        return int(m.json().get('size', 0)), fid

def delete_media_file(uuid, fid):
    r = requests.delete(f'https://api.zoom.us/v2/meetings/{enc(uuid)}/recordings/{fid}',
                        params={'action': 'trash'}, headers={'Authorization': f'Bearer {ztok()}'}, timeout=60)
    return r.status_code, r.text[:120]

def process(mt):
    """Back up all files, verify media, delete media. Returns (backed, deleted, gb)."""
    ts = (mt['start_time'] or '').replace('T', ' ').replace('Z', '')
    host = mt.get('host_email') or mt.get('host') or 'unknown'
    hf = folder(sanitize(host), ROOT)
    mf = folder(sanitize(f"{ts}: {mt.get('topic','Untitled')}"), hf)
    present = folder_names(mf)
    files = [f for f in mt.get('recording_files', []) if f.get('status') == 'completed']
    backed = deleted = 0; gb = 0
    verified_media = []
    for f in files:
        ftype = f.get('file_type')
        ext = (f.get('file_extension') or EXT.get(ftype, 'bin')).lower().lstrip('.')
        name = sanitize(f"{ts} {mt.get('topic','')} ({f.get('recording_type', ftype)})") + f'.{ext}'
        size = f.get('file_size', 0)
        if name in present:
            if ftype in MEDIA: verified_media.append(f['id'])  # already backed up
            continue
        if DRY_RUN:
            print(f"    [dry] would back up {name} ({size/1e6:.0f}MB)", flush=True); backed += 1; continue
        try:
            got, _ = stream_to_drive(f['download_url'], name, mf)
            if ftype in MEDIA and size and got != size:
                raise RuntimeError(f'size mismatch {size} vs {got}')
            backed += 1; gb += got
            if ftype in MEDIA: verified_media.append(f['id'])
        except Exception as e:
            print(f"    BACKUP FAIL {name[:50]}: {str(e)[:80]}", flush=True)
    # delete only media whose backup is verified
    for f in files:
        if f.get('file_type') in MEDIA and f['id'] in verified_media:
            if DRY_RUN:
                print(f"    [dry] would delete media {f.get('recording_type')}", flush=True); deleted += 1; continue
            sc, msg = delete_media_file(mt['uuid'], f['id'])
            if sc in (200, 204): deleted += 1
            else: print(f"    DELETE FAIL {f['id']}: {sc} {msg}", flush=True)
    return backed, deleted, gb

def process_gap(mt):
    """Backfill only the media files MISSING from the Drive folder, matched by
    byte size (collision-safe for 3-day meetings with repeated layouts). Uploads
    with a recording_start-uniquified name. Does NOT delete from Zoom. Retries
    each file to ride out Drive rate-limits. Returns (uploaded, missing_total)."""
    from collections import Counter
    ts = (mt['start_time'] or '').replace('T', ' ').replace('Z', '')
    host = mt.get('host_email') or mt.get('host') or 'unknown'
    hf = folder(sanitize(host), ROOT)
    mf = folder(sanitize(f"{ts}: {mt.get('topic','Untitled')}"), hf)
    zmedia = [f for f in mt.get('recording_files', [])
              if f.get('file_type') in MEDIA and f.get('status') == 'completed' and f.get('file_size', 0) >= 1_000_000]
    have = Counter(s for _, s in folder_files(mf) if s >= 1_000_000)
    missing = []
    for f in sorted(zmedia, key=lambda x: -x.get('file_size', 0)):
        if have[f['file_size']] > 0: have[f['file_size']] -= 1
        else: missing.append(f)
    print(f"  {len(missing)} missing / {len(zmedia)} media", flush=True)
    ok = 0
    for f in missing:
        ext = (f.get('file_extension') or EXT.get(f['file_type'], 'bin')).lower().lstrip('.')
        rs = (f.get('recording_start') or '').replace('T', '_').replace('Z', '').replace(':', '-')
        name = sanitize(f"{ts} {mt.get('topic','')} ({f.get('recording_type', f['file_type'])}) [{rs}]") + f'.{ext}'
        size = f.get('file_size', 0)
        for attempt in range(6):
            try:
                got, fid = stream_to_drive(f['download_url'], name, mf)
                if size and got != size:
                    requests.delete(f'{DRIVE_API}/{fid}', headers=dh(), params={'supportsAllDrives': 'true'}, timeout=60)
                    raise RuntimeError(f'size mismatch {size} vs {got}')
                ok += 1; print(f"    ok {name[:60]} {got/1e6:.0f}MB", flush=True); break
            except Exception as e:
                if attempt == 5:
                    print(f"    FAIL {name[:50]}: {str(e)[:80]}", flush=True)
                else:
                    time.sleep(min(90, 5 * (2 ** attempt)))
    return ok, len(missing)

def users():
    out, np = [], ''
    while True:
        p = {'page_size': 300, 'status': 'active'}
        if np: p['next_page_token'] = np
        d = requests.get('https://api.zoom.us/v2/users', headers={'Authorization': f'Bearer {ztok()}'}, params=p, timeout=60).json()
        out += d.get('users', []); np = d.get('next_page_token', '')
        if not np: return out

def main():
    print(f"MODE={MODE} DRY_RUN={DRY_RUN}", flush=True)
    meetings = []
    if MODE == 'gap':
        # GAP_TARGETS = JSON list of {"host","date","uuid"}. Backfill only missing
        # media (size-matched), no deletion. For finishing partial old meetings.
        targets = json.loads(os.environ.get('GAP_TARGETS', '[]'))
        umap = {u['email']: u['id'] for u in users()}
        tot_ok = tot_miss = 0
        for t in targets:
            uid = umap.get(t['host'])
            if not uid:
                print(f"[gap] {t['date']} host {t['host']} not found", flush=True); continue
            d = requests.get(f"https://api.zoom.us/v2/users/{uid}/recordings",
                             headers={'Authorization': f'Bearer {ztok()}'},
                             params={'from': t['date'], 'to': t['date'], 'page_size': 300}, timeout=60).json()
            mt = next((m for m in d.get('meetings', []) if m['uuid'] == t['uuid']), None)
            if not mt:
                print(f"[gap] {t['date']} {t['uuid']} not found live", flush=True); continue
            mt['host_email'] = t['host']
            print(f"[gap] {t['date']} {mt.get('topic','')[:45]}", flush=True)
            o, m = process_gap(mt); tot_ok += o; tot_miss += m
        print(f"\nDONE gap. uploaded={tot_ok}/{tot_miss} missing files", flush=True)
        return
    if MODE == 'single':
        # fetch the one meeting across users (webhook gives us the uuid)
        for u in users():
            d = requests.get(f"https://api.zoom.us/v2/users/{u['id']}/recordings",
                             headers={'Authorization': f'Bearer {ztok()}'},
                             params={'from': (datetime.now()-timedelta(days=3)).strftime('%Y-%m-%d'),
                                     'to': datetime.now().strftime('%Y-%m-%d'), 'page_size': 300}, timeout=60).json()
            for m in d.get('meetings', []):
                if m['uuid'] == MEETING_UUID:
                    m['host_email'] = u['email']; meetings.append(m)
    else:  # sweep
        frm = (datetime.now() - timedelta(days=DAYS)).strftime('%Y-%m-%d')
        to = datetime.now().strftime('%Y-%m-%d')
        for u in users():
            d = requests.get(f"https://api.zoom.us/v2/users/{u['id']}/recordings",
                             headers={'Authorization': f'Bearer {ztok()}'},
                             params={'from': frm, 'to': to, 'page_size': 300}, timeout=60).json()
            for m in d.get('meetings', []):
                m['host_email'] = u['email']; meetings.append(m)
    print(f"{len(meetings)} meeting(s) to process", flush=True)
    tb = td = 0; tgb = 0
    for m in meetings:
        print(f"[{m['start_time'][:10]}] {m.get('host_email')} {m.get('topic','')[:40]}", flush=True)
        b, d, gb = process(m); tb += b; td += d; tgb += gb
    print(f"\nDONE. backed_up={tb} files ({tgb/1e9:.1f} GB), media_deleted={td}", flush=True)

if __name__ == '__main__':
    main()
