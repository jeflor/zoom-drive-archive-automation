"""Delete RA-locked meetings via the whole-meeting endpoint (bypasses the
per-file API bug). Gated: only delete a meeting if ALL its media (MP4/M4A>=1MB)
is verified present in Drive. Resumable via wm_deleted.txt. Trash (recoverable)."""
import sys, json, base64, time, urllib.parse, requests, os
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG
zoom=json.load(open('zoom_recordings.json'))
drive=json.load(open('drive_index_md5.json'))
deleted=set(l.strip() for l in open('deleted_log.txt') if l.strip())
MEDIA={'MP4','M4A'}; gsize=Counter(f['size'] for f in drive if f['size']>=1_000_000)
done=set()
if os.path.exists('wm_deleted.txt'): done=set(l.strip() for l in open('wm_deleted.txt') if l.strip())

c=base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()
def token():
    return requests.post('https://zoom.us/oauth/token',params={'grant_type':'account_credentials','account_id':CONFIG['ZOOM_ACCOUNT_ID']},headers={'Authorization':f'Basic {c}'},timeout=60).json()['access_token']
tok=token()
def enc(u): return urllib.parse.quote(urllib.parse.quote(u,safe=''),safe='') if (u.startswith('/') or '//' in u) else urllib.parse.quote(u,safe='')

# build RA-locked, media-verified target list
targets=[]
for m in zoom:
    if m['uuid'] in done: continue
    live=[f for f in m['files'] if f['file_type'] in MEDIA and f['size']>=1_000_000 and f['id'] not in deleted]
    if not live: continue
    av=Counter(gsize); miss=0
    for f in sorted(live,key=lambda x:-x['size']):
        if av[f['size']]>0: av[f['size']]-=1
        else: miss+=1
    if miss==0: targets.append(m)
print(f"targets: {len(targets)} RA-locked, media-verified meetings",flush=True)
wl=open('wm_deleted.txt','a'); dl=open('deleted_log.txt','a')
ok=0; freed=0; fail=0
for i,m in enumerate(targets):
    u=m['uuid']
    for attempt in range(5):
        r=requests.delete(f'https://api.zoom.us/v2/meetings/{enc(u)}/recordings',params={'action':'trash'},headers={'Authorization':f'Bearer {tok}'},timeout=60)
        if r.status_code==401: tok=token(); continue
        if r.status_code==429: time.sleep(int(r.headers.get('Retry-After',5))); continue
        break
    if r.status_code in (200,204):
        ok+=1; freed+=sum(f['size'] for f in m['files'])
        wl.write(u+'\n'); wl.flush()
        for f in m['files']: dl.write(f['id']+'\n')
        dl.flush()
    else:
        fail+=1; print(f"  FAIL {m['start_time'][:10]} {m['topic'][:30]}: {r.status_code} {r.text[:90]}",flush=True)
    if ok and ok%25==0: print(f"  ...{ok} meetings deleted, {freed/1e9:.1f} GB",flush=True)
    time.sleep(0.2)
print(f"\nDONE. deleted={ok} meetings ({freed/1e9:.1f} GB), failed={fail}",flush=True)
