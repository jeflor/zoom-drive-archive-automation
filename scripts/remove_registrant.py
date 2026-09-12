#!/usr/bin/env python3
"""
Hard-remove registrants from the recurring registration-enabled meeting by email.

Hard delete = NO cancellation email is sent (uses meeting:delete:registrant:admin).
Matches emails (case-insensitive) against the CURRENT registrant list across all
statuses (approved/pending/denied), deletes any match, and appends an audit row to
exports/registrant_removal_log.csv.

Usage:
    ./venv/bin/python scripts/remove_registrant.py a@x.com b@y.com ...
    ./venv/bin/python scripts/remove_registrant.py --file emails.txt   # one per line

Meeting: 1234567890 (training@example.com). Override with MEETING_ID env var.
"""
import base64, requests, sys, csv, os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import CONFIG

MID = os.environ.get('MEETING_ID', '1234567890')
LOG = os.path.join(os.path.dirname(__file__), '..', 'exports', 'registrant_removal_log.csv')


def ztok():
    b = base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()
    r = requests.post('https://zoom.us/oauth/token',
                      params={'grant_type': 'account_credentials', 'account_id': CONFIG['ZOOM_ACCOUNT_ID']},
                      headers={'Authorization': f'Basic {b}'}, timeout=60)
    r.raise_for_status()
    return r.json()['access_token']


def current_registrants(zh):
    m = {}
    for st in ('approved', 'pending', 'denied'):
        page = None
        while True:
            p = {'status': st, 'page_size': 300}
            if page:
                p['next_page_token'] = page
            j = requests.get(f'https://api.zoom.us/v2/meetings/{MID}/registrants',
                             headers=zh, params=p, timeout=60).json()
            for r in j.get('registrants', []):
                m[(r.get('email') or '').strip().lower()] = r
            page = j.get('next_page_token')
            if not page:
                break
    return m


def parse_args(argv):
    emails = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--file':
            i += 1
            with open(argv[i]) as fh:
                emails += [ln.strip() for ln in fh if ln.strip()]
        else:
            emails.append(a)
        i += 1
    # normalize + dedupe, preserve order
    seen, out = set(), []
    for e in emails:
        k = e.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def main():
    emails = parse_args(sys.argv[1:])
    if not emails:
        print('no emails given'); return
    zh = {'Authorization': f'Bearer {ztok()}'}
    cur = current_registrants(zh)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    new_log = not os.path.exists(LOG)
    deleted = notfound = failed = 0
    with open(LOG, 'a', newline='') as fh:
        w = csv.writer(fh)
        if new_log:
            w.writerow(['timestamp', 'email', 'result', 'registrant_id', 'name', 'prior_status'])
        for e in emails:
            r = cur.get(e)
            if not r:
                print(f'  NOT REGISTERED  {e}')
                w.writerow([ts, e, 'NOT_REGISTERED', '', '', ''])
                notfound += 1
                continue
            rid = r.get('id')
            name = f"{r.get('first_name','')} {r.get('last_name','')}".strip()
            resp = requests.delete(f'https://api.zoom.us/v2/meetings/{MID}/registrants/{rid}',
                                   headers=zh, timeout=60)
            if resp.status_code in (200, 204):
                print(f'  DELETED         {e}  ({name})')
                w.writerow([ts, e, 'DELETED', rid, name, r.get('status', '')])
                deleted += 1
            else:
                print(f'  FAILED {resp.status_code}   {e}  {resp.text[:100]}')
                w.writerow([ts, e, f'FAIL_{resp.status_code}', rid, name, r.get('status', '')])
                failed += 1
    print(f'\ndeleted={deleted} not_registered={notfound} failed={failed}  (log: exports/registrant_removal_log.csv)')


if __name__ == '__main__':
    main()
