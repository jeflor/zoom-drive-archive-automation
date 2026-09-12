"""Trash a set of Drive files by id (move to Trash, recoverable ~30 days).
Runtime guard: verifies each file's md5 keeper still exists & is not trashed
before trashing. Resumable via drive_trashed_log.txt. Read args:
  --group-billwilliams  : only the most-duplicated Bill Williams md5 group
  (default)             : all ids in dedupe_delete_ids.json
"""
import os
import sys, json, time
from collections import defaultdict
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

files=json.load(open('drive_index_md5.json'))
del_ids=set(json.load(open('dedupe_delete_ids.json')))
by_id={f['id']:f for f in files}
by_md5=defaultdict(list)
for f in files:
    if f['md5']: by_md5[f['md5']].append(f)
keeper_of={}  # md5 -> keeper id (the one NOT in del_ids)
for md5,fs in by_md5.items():
    keep=[f for f in fs if f['id'] not in del_ids]
    if keep: keeper_of[md5]=keep[0]['id']

if '--group-billwilliams' in sys.argv:
    # pick the md5 with the most copies whose name mentions Bill Williams
    cand=sorted(by_md5.items(), key=lambda x:-len(x[1]))
    target=next(m for m,fs in cand if 'bill williams' in fs[0]['name'].lower())
    targets=[f['id'] for f in by_md5[target] if f['id'] in del_ids]
    print(f"Target md5 {target[:12]}... : {len(by_md5[target])} copies, trashing {len(targets)}, keeping 1")
    print(f"KEEPING: {by_id[keeper_of[target]]['path']}  [{keeper_of[target]}]")
else:
    targets=list(del_ids)

creds=Credentials.from_authorized_user_file(os.environ.get('DRIVE_TOKEN_JSON', 'token.json'),['https://www.googleapis.com/auth/drive'])
if not creds.valid: creds.refresh(Request())
svc=build('drive','v3',credentials=creds,cache_discovery=False)

done=set()
try: done=set(l.strip() for l in open('drive_trashed_log.txt') if l.strip())
except FileNotFoundError: pass
dlog=open('drive_trashed_log.txt','a')
n=0; freed=0; skipped=0; fail=0
keeper_ok={}
for fid in targets:
    if fid in done: skipped+=1; continue
    f=by_id.get(fid)
    if not f: fail+=1; continue
    md5=f['md5']; keep=keeper_of.get(md5)
    if not keep or keep==fid:
        print(f"  GUARD: skip {fid} (no distinct keeper)"); fail+=1; continue
    # runtime guard: keeper must exist & not be trashed
    if keep not in keeper_ok:
        try:
            meta=svc.files().get(fileId=keep, fields='id,trashed', supportsAllDrives=True).execute()
            keeper_ok[keep]= not meta.get('trashed', False)
        except HttpError:
            keeper_ok[keep]=False
    if not keeper_ok[keep]:
        print(f"  GUARD: keeper missing for {fid}, NOT trashing"); fail+=1; continue
    for attempt in range(5):
        try:
            svc.files().update(fileId=fid, body={'trashed':True}, supportsAllDrives=True).execute()
            dlog.write(fid+'\n'); dlog.flush(); n+=1; freed+=f['size']; break
        except HttpError as e:
            if attempt==4: print(f"  FAIL {fid}: {e}"); fail+=1
            else: time.sleep(2)
    if n and n%50==0: print(f"  ...{n} trashed, {freed/1e9:.1f} GB",flush=True)
    time.sleep(0.1)
print(f"\nDONE. trashed={n} ({freed/1e9:.2f} GB), skipped={skipped}, failed={fail}")
