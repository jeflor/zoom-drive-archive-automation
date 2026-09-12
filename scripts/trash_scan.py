"""Read-only: measure what's in Zoom TRASH (trashed=true) vs live, per host,
to reconcile against the account storage meter. Reads creds from project config."""
import sys, base64, requests
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG  # noqa: E402

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


def walk(uid, trashed):
    total = 0
    n = 0
    seen = set()
    cur, end = datetime(2023, 1, 1), datetime.now()
    while cur <= end:
        ce = min(cur + timedelta(days=30), end)
        np = ''
        while True:
            p = {'from': cur.strftime('%Y-%m-%d'), 'to': ce.strftime('%Y-%m-%d'), 'page_size': 300}
            if trashed:
                p['trashed'] = 'true'
            if np:
                p['next_page_token'] = np
            global tok
            resp = requests.get(f'https://api.zoom.us/v2/users/{uid}/recordings', headers=H(), params=p)
            if resp.status_code == 401:
                tok = token()
                continue
            if resp.status_code != 200:
                break
            d = resp.json()
            for m in d.get('meetings', []):
                if m['uuid'] in seen:
                    continue
                seen.add(m['uuid'])
                total += m.get('total_size', 0)
                n += 1
            np = d.get('next_page_token', '')
            if not np:
                break
        cur = ce + timedelta(days=1)
    return n, total


print(f"{'host':30} {'LIVE n':>7} {'LIVE GB':>9} {'TRASH n':>8} {'TRASH GB':>9}")
tl = tlb = tt = ttb = 0
for u in users:
    ln, lb = walk(u['id'], False)
    tn, tb = walk(u['id'], True)
    if ln or tn:
        print(f"{u['email']:30} {ln:7} {lb/1e9:9.1f} {tn:8} {tb/1e9:9.1f}")
    tl += ln; tlb += lb; tt += tn; ttb += tb
print(f"{'TOTAL':30} {tl:7} {tlb/1e9:9.1f} {tt:8} {ttb/1e9:9.1f}")
print(f"\nLIVE + TRASH = {(tlb+ttb)/1e9:.1f} GB")
