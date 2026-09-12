"""Back up Zoom recordings to Drive zBackup, verifying bytes after upload.
Streams via a temp file (deleted immediately) - no permanent local copy.
Uploads ALL files (media + transcripts) so the Drive backup is complete.
  --limit N       process only N meetings
  --smallest      process smallest meetings first (for pilot)
Resumable: uploaded Zoom file ids logged to backed_up_log.txt
"""
import sys, os, json, time, base64, tempfile, re
import requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ZROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
LIMIT=int(sys.argv[sys.argv.index('--limit')+1]) if '--limit' in sys.argv else None
work=json.load(open('backup_worklist.json'))
if '--smallest' in sys.argv:
    work.sort(key=lambda w: sum(f['size'] for f in w['missing_media']))
if '--min-mb' in sys.argv:
    _m=float(sys.argv[sys.argv.index('--min-mb')+1])*1e6
    work=[w for w in work if sum(f['size'] for f in w['missing_media'])>=_m]
    work.sort(key=lambda w: sum(f['size'] for f in w['missing_media']))

done=set()
if os.path.exists('backed_up_log.txt'):
    done=set(l.strip() for l in open('backed_up_log.txt') if l.strip())
blog=open('backed_up_log.txt','a')

_c=base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()
def ztoken():
    r=requests.post('https://zoom.us/oauth/token',
        params={'grant_type':'account_credentials','account_id':CONFIG['ZOOM_ACCOUNT_ID']},
        headers={'Authorization':f'Basic {_c}'},timeout=60)
    r.raise_for_status(); return r.json()['access_token']
tok=ztoken()

creds=Credentials.from_authorized_user_file(os.environ.get('DRIVE_TOKEN_JSON', 'token.json'),['https://www.googleapis.com/auth/drive'])
if not creds.valid: creds.refresh(Request())
svc=build('drive','v3',credentials=creds,cache_discovery=False)

def sanitize(s): return re.sub(r'[\\/:*?"<>|]','_',s)[:120]
folder_cache={}
def get_folder(name,parent):
    key=(parent,name)
    if key in folder_cache: return folder_cache[key]
    q=(f"name='{name.replace(chr(39), chr(92)+chr(39))}' and '{parent}' in parents "
       f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    r=svc.files().list(q=q,fields='files(id)',supportsAllDrives=True,
        includeItemsFromAllDrives=True,corpora='allDrives').execute().get('files',[])
    fid=r[0]['id'] if r else svc.files().create(
        body={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]},
        fields='id',supportsAllDrives=True).execute()['id']
    folder_cache[key]=fid; return fid

# fresh download urls for a meeting via the account-level listing
users={}
def user_id(email):
    global users
    if not users:
        r=requests.get('https://api.zoom.us/v2/users',headers={'Authorization':f'Bearer {tok}'},
            params={'page_size':300,'status':'active'},timeout=60).json()
        users={u['email']:u['id'] for u in r.get('users',[])}
    return users.get(email)

EXT={'MP4':'mp4','M4A':'m4a','CHAT':'txt','TRANSCRIPT':'vtt','TIMELINE':'json','CC':'vtt','SUMMARY':'rtf','CSV':'csv'}
tot_bytes=0; t_start=time.time(); n_files=0; n_fail=0
for wi,w in enumerate(work):
    if LIMIT is not None and wi>=LIMIT: break
    uid=user_id(w['host'])
    if not uid: print(f"  no user id for {w['host']}"); continue
    d=w['date']
    r=requests.get(f'https://api.zoom.us/v2/users/{uid}/recordings',
        headers={'Authorization':f'Bearer {tok}'},params={'from':d,'to':d,'page_size':300},timeout=60)
    if r.status_code!=200: print(f"  list failed {r.status_code}"); continue
    mt=next((x for x in r.json().get('meetings',[]) if x['uuid']==w['uuid']),None)
    if not mt: print(f"  meeting not found live: {w['date']} {w['topic'][:40]}"); continue
    host_folder=get_folder(sanitize(w['host']),ZROOT)
    ts=(w['start_time'] or '').replace('T',' ').replace('Z','')
    mfolder=get_folder(sanitize(f"{ts}: {w['topic']}"),host_folder)
    print(f"\n[{wi+1}] {w['date']} {w['topic'][:50]} -> {w['host']}")
    for f in mt.get('recording_files',[]):
        if f.get('status')!='completed': continue
        fid=f['id']
        if fid in done: continue
        size=f.get('file_size',0)
        ext=(f.get('file_extension') or EXT.get(f.get('file_type'),'bin')).lower().lstrip('.')
        name=sanitize(f"{ts} {w['topic']} ({f.get('recording_type',f.get('file_type'))})")+f".{ext}"
        url=f['download_url']
        t0=time.time()
        try:
            with requests.get(url,headers={'Authorization':f'Bearer {tok}'},stream=True,timeout=300) as resp:
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    for chunk in resp.iter_content(chunk_size=8*1024*1024):
                        if chunk: tmp.write(chunk)
                    tpath=tmp.name
            up=svc.files().create(body={'name':name,'parents':[mfolder]},
                media_body=MediaFileUpload(tpath,resumable=True),
                fields='id,size',supportsAllDrives=True).execute()
            got=int(up.get('size',0))
            os.remove(tpath)
            if size and got!=size:
                print(f"    MISMATCH {name}: zoom={size} drive={got}"); n_fail+=1; continue
            blog.write(fid+'\n'); blog.flush()
            dt=time.time()-t0; tot_bytes+=got; n_files+=1
            print(f"    ok {name[:60]} {got/1e6:.0f}MB in {dt:.0f}s ({got/1e6/max(dt,1):.1f} MB/s)")
        except Exception as e:
            print(f"    ERROR {name[:50]}: {str(e)[:120]}"); n_fail+=1
            try: os.remove(tpath)
            except: pass
el=time.time()-t_start
print(f"\nDONE. files={n_files} fail={n_fail} bytes={tot_bytes/1e9:.2f}GB in {el/60:.1f}min "
      f"avg={tot_bytes/1e6/max(el,1):.1f} MB/s")
