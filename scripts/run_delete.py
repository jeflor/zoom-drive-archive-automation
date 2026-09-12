"""Trash Zoom media files (MP4/M4A) for backed-up meetings, keeping transcripts.
- action=trash (recoverable ~30 days)
- gate: each file's size must be present in its Drive backup folder (from today's Drive API scan)
- resumable: deleted file ids appended to deleted_log.txt, skipped on rerun
- --limit N : only process N files (used for the 1-file scope proof)
Reads Zoom creds from project config; no secrets hardcoded."""
import sys, os, json, time, base64, urllib.parse
from collections import defaultdict, Counter
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG

HERE = os.environ.get('WORK_DIR', 'data')
LIMIT = None
if '--limit' in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index('--limit')+1])

batch = json.load(open(f'{HERE}/batch_delete.json'))
drive = json.load(open(f'{HERE}/drive_index.json'))
folder_sizes = defaultdict(Counter)
for f in drive['files']:
    folder_sizes[f['path']][f['size']] += 1

done = set()
if os.path.exists(f'{HERE}/deleted_log.txt'):
    done = set(l.strip() for l in open(f'{HERE}/deleted_log.txt') if l.strip())

_c = base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()
def get_token():
    r = requests.post('https://zoom.us/oauth/token',
        params={'grant_type':'account_credentials','account_id':CONFIG['ZOOM_ACCOUNT_ID']},
        headers={'Authorization': f'Basic {_c}'}, timeout=60)
    r.raise_for_status(); return r.json()['access_token']
token = get_token()

def enc(uuid):
    if uuid.startswith('/') or '//' in uuid:
        return urllib.parse.quote(urllib.parse.quote(uuid, safe=''), safe='')
    return urllib.parse.quote(uuid, safe='')

dellog = open(f'{HERE}/deleted_log.txt','a')
faillog = open(f'{HERE}/delete_failures.log','a')
n_del=n_skip=n_fail=freed=0; processed=0; first_done=False

def verify_trashed(uuid, file_id):
    r = requests.get(f'https://api.zoom.us/v2/meetings/{enc(uuid)}/recordings',
                     headers={'Authorization': f'Bearer {token}'}, timeout=60)
    if r.status_code != 200: return None
    live_ids = {f['id'] for f in r.json().get('recording_files', [])}
    return file_id not in live_ids  # gone from live listing => trashed

for mt in batch:
    uuid = mt['uuid']; path = mt['drive_folder']
    for f in mt['files']:
        if LIMIT is not None and processed >= LIMIT: break
        fid = f['id']
        if fid in done: n_skip += 1; continue
        # Drive backup gate
        if folder_sizes[path][f['size']] <= 0:
            faillog.write(f"NO_DRIVE_MATCH {uuid} {fid} size={f['size']} {path}\n"); faillog.flush()
            n_fail += 1; continue
        # delete to trash
        processed += 1
        for attempt in range(6):
            r = requests.delete(f'https://api.zoom.us/v2/meetings/{enc(uuid)}/recordings/{fid}',
                                params={'action':'trash'},
                                headers={'Authorization': f'Bearer {token}'}, timeout=60)
            if r.status_code == 401:
                token = get_token(); continue
            if r.status_code == 429:
                wait = int(r.headers.get('Retry-After', 5)); time.sleep(min(wait, 30)); continue
            break
        if r.status_code == 400 and '4711' in r.text:
            print(f"ABORT: Zoom app still missing delete scope (code 4711). "
                  f"Add cloud_recording:delete:recording_file:admin and retry.", flush=True)
            sys.exit(2)
        if r.status_code in (200, 204):
            dellog.write(fid+'\n'); dellog.flush()
            n_del += 1; freed += f['size']
            if not first_done:
                first_done = True
                ok = verify_trashed(uuid, fid)
                print(f"PROOF: deleted 1 file (HTTP {r.status_code}). "
                      f"Re-checked meeting live recordings: file now absent = {ok}", flush=True)
                print(f"  meeting: {mt['date']} {mt['topic'][:50]}  type={f['recording_type']} "
                      f"size={f['size']/1e6:.0f}MB", flush=True)
        elif r.status_code in (403,):
            print(f"ABORT: HTTP 403 — Zoom app lacks recording:write (delete) scope. "
                  f"Body: {r.text[:200]}", flush=True)
            sys.exit(2)
        elif r.status_code == 404:
            dellog.write(fid+'\n'); dellog.flush(); n_skip += 1  # already gone
        else:
            faillog.write(f"HTTP{r.status_code} {uuid} {fid} {r.text[:150]}\n"); faillog.flush()
            n_fail += 1
        if n_del and n_del % 100 == 0:
            print(f"  ...{n_del} trashed, {freed/1e9:.1f} GB, {n_fail} fail, {n_skip} skip", flush=True)
        time.sleep(0.15)
    if LIMIT is not None and processed >= LIMIT: break

print(f"\nDONE. trashed={n_del} ({freed/1e9:.1f} GB), skipped={n_skip}, failed={n_fail}", flush=True)
