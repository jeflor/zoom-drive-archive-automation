"""Read-only walk of everything currently in Zoom cloud (not trashed).
Records each meeting with its per-file byte sizes and UUIDs, so we can
verify against Drive and later delete by UUID. Writes zoom_recordings.json.
Reads Zoom creds from the existing project config, not hardcoded."""
import sys, json, base64, time, requests
from datetime import datetime, timedelta
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG  # noqa: E402

_sess = requests.Session()
_sess.mount('https://', HTTPAdapter(max_retries=Retry(
    total=6, connect=6, read=6, backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET', 'POST'])))
requests = _sess  # transparently reuse the retrying session for .get/.post below

OUT = Path(__file__).parent / 'zoom_recordings.json'

c = base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()


def token():
    r = requests.post('https://zoom.us/oauth/token',
                      params={'grant_type': 'account_credentials', 'account_id': CONFIG['ZOOM_ACCOUNT_ID']},
                      headers={'Authorization': f'Basic {c}'})
    r.raise_for_status()
    return r.json()['access_token']


tok = token()
H = lambda: {'Authorization': f'Bearer {tok}'}

users, np = [], ''
while True:
    p = {'page_size': 300, 'status': 'active'}
    if np:
        p['next_page_token'] = np
    d = requests.get('https://api.zoom.us/v2/users', headers=H(), params=p).json()
    users += d.get('users', [])
    np = d.get('next_page_token', '')
    if not np:
        break
print(f"{len(users)} active users", flush=True)

meetings = []
start, end = datetime(2019, 1, 1), datetime.now()
for u in users:
    cur = start
    ucount = 0
    while cur <= end:
        ce = min(cur + timedelta(days=30), end)
        np = ''
        while True:
            p = {'from': cur.strftime('%Y-%m-%d'), 'to': ce.strftime('%Y-%m-%d'), 'page_size': 300}
            if np:
                p['next_page_token'] = np
            resp = requests.get(f"https://api.zoom.us/v2/users/{u['id']}/recordings", headers=H(), params=p)
            if resp.status_code == 401:
                tok = token()
                continue
            if resp.status_code != 200:
                print(f"  {u['email']} {cur:%Y-%m}: HTTP {resp.status_code}", flush=True)
                break
            d = resp.json()
            for m in d.get('meetings', []):
                recs = []
                for f in m.get('recording_files', []):
                    if f.get('status') != 'completed':
                        continue
                    recs.append({
                        'id': f['id'],
                        'file_type': f.get('file_type'),
                        'recording_type': f.get('recording_type'),
                        'size': f.get('file_size', 0),
                    })
                meetings.append({
                    'host': u['email'],
                    'uuid': m['uuid'],
                    'meeting_id': m.get('id'),
                    'topic': m.get('topic', ''),
                    'start_time': m.get('start_time'),
                    'total_size': m.get('total_size', 0),
                    'files': recs,
                })
                ucount += 1
            np = d.get('next_page_token', '')
            if not np:
                break
        cur = ce + timedelta(days=1)
    print(f"  {u['email']}: {ucount} meetings", flush=True)

json.dump(meetings, open(OUT, 'w'))
tot = sum(m['total_size'] for m in meetings)
print(f"Done. {len(meetings)} meetings, {tot/1e9:.1f} GB currently in Zoom cloud. Wrote {OUT.name}")
