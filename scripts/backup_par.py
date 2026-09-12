"""Parallel Zoom->Drive backup with strict media verification.
- N worker threads (each own Drive client; httplib2 isn't thread-safe)
- MEDIA (MP4/M4A): uploaded size MUST equal Zoom's; mismatch => delete orphan, retry later
- metadata (summary/timeline/cc/chat/transcript): Zoom's size field is unreliable -> accept
- skips files already present in the target folder (no duplicate creation)
- resumable via backed_up_log.txt
  --limit N   meetings   --workers N   --min-mb X
"""
import sys, os, json, time, base64, tempfile, re, threading
import requests
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import CONFIG
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ZROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
MEDIA={'MP4','M4A'}
arg=lambda k,d=None: (sys.argv[sys.argv.index(k)+1] if k in sys.argv else d)
LIMIT=int(arg('--limit')) if arg('--limit') else None
WORKERS=int(arg('--workers','6'))
work=json.load(open('backup_worklist.json'))
if arg('--min-mb'):
    m=float(arg('--min-mb'))*1e6
    work=[w for w in work if sum(f['size'] for f in w['missing_media'])>=m]
    work.sort(key=lambda w:sum(f['size'] for f in w['missing_media']))
if LIMIT: work=work[:LIMIT]

lock=threading.Lock()
done=set()
if os.path.exists('backed_up_log.txt'):
    done=set(l.strip() for l in open('backed_up_log.txt') if l.strip())
blog=open('backed_up_log.txt','a')

_c=base64.b64encode(f"{CONFIG['ZOOM_CLIENT_ID']}:{CONFIG['ZOOM_CLIENT_SECRET']}".encode()).decode()
_tok={'v':None,'t':0}
def ztoken():
    with lock:
        if _tok['v'] and time.time()-_tok['t']<2400: return _tok['v']
        r=requests.post('https://zoom.us/oauth/token',
            params={'grant_type':'account_credentials','account_id':CONFIG['ZOOM_ACCOUNT_ID']},
            headers={'Authorization':f'Basic {_c}'},timeout=60)
        r.raise_for_status(); _tok['v']=r.json()['access_token']; _tok['t']=time.time()
        return _tok['v']

_creds=Credentials.from_authorized_user_file(os.environ.get('DRIVE_TOKEN_JSON', 'token.json'),['https://www.googleapis.com/auth/drive'])
if not _creds.valid: _creds.refresh(Request())
_local=threading.local()
def drive():
    if not hasattr(_local,'svc'):
        _local.svc=build('drive','v3',credentials=_creds,cache_discovery=False)
    return _local.svc

def sanitize(s): return re.sub(r'[\\/:*?"<>|]','_',s)[:120]
fcache={}
def get_folder(name,parent):
    key=(parent,name)
    with lock:
        if key in fcache: return fcache[key]
    q=(f"name='{name.replace(chr(39),chr(92)+chr(39))}' and '{parent}' in parents "
       f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    r=drive().files().list(q=q,fields='files(id)',supportsAllDrives=True,
        includeItemsFromAllDrives=True,corpora='allDrives').execute().get('files',[])
    fid=r[0]['id'] if r else drive().files().create(
        body={'name':name,'mimeType':'application/vnd.google-apps.folder','parents':[parent]},
        fields='id',supportsAllDrives=True).execute()['id']
    with lock: fcache[key]=fid
    return fid

EXT={'MP4':'mp4','M4A':'m4a','CHAT':'txt','TRANSCRIPT':'vtt','TIMELINE':'json','CC':'vtt','SUMMARY':'json','CSV':'csv'}
stats={'files':0,'bytes':0,'fail':0,'skip':0}

def existing_names(folder):
    out=set(); tok=None
    while True:
        r=drive().files().list(q=f"'{folder}' in parents and trashed=false",
            fields='nextPageToken, files(name)',pageToken=tok,pageSize=1000,
            supportsAllDrives=True,includeItemsFromAllDrives=True,corpora='allDrives').execute()
        out|={x['name'] for x in r.get('files',[])}
        tok=r.get('nextPageToken')
        if not tok: break
    return out

def do_file(job):
    f,folder,ts,topic,present=job
    fid=f['id']
    if fid in done:
        with lock: stats['skip']+=1
        return
    size=f.get('file_size',0)
    ftype=f.get('file_type')
    ext=(f.get('file_extension') or EXT.get(ftype,'bin')).lower().lstrip('.')
    name=sanitize(f"{ts} {topic} ({f.get('recording_type',ftype)})")+f".{ext}"
    if name in present:   # already uploaded by a prior partial run
        with lock:
            blog.write(fid+'\n'); blog.flush(); stats['skip']+=1
        return
    tpath=None
    try:
        tk=ztoken()
        with requests.get(f['download_url'],headers={'Authorization':f'Bearer {tk}'},
                          stream=True,timeout=600) as resp:
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                for ch in resp.iter_content(chunk_size=8*1024*1024):
                    if ch: tmp.write(ch)
                tpath=tmp.name
        up=drive().files().create(body={'name':name,'parents':[folder]},
            media_body=MediaFileUpload(tpath,resumable=True),
            fields='id,size',supportsAllDrives=True).execute()
        got=int(up.get('size',0))
        if ftype in MEDIA and size and got!=size:
            # strict for media: remove the bad upload so we never keep a partial copy
            try: drive().files().delete(fileId=up['id'],supportsAllDrives=True).execute()
            except Exception: pass
            with lock:
                stats['fail']+=1
                print(f"    MISMATCH(media) {name[:50]} zoom={size} drive={got} -> orphan removed",flush=True)
            return
        with lock:
            blog.write(fid+'\n'); blog.flush()
            stats['files']+=1; stats['bytes']+=got
    except Exception as e:
        with lock:
            stats['fail']+=1; print(f"    ERROR {name[:45]}: {str(e)[:100]}",flush=True)
    finally:
        if tpath:
            try: os.remove(tpath)
            except Exception: pass

# build jobs
users={}
r=requests.get('https://api.zoom.us/v2/users',headers={'Authorization':f'Bearer {ztoken()}'},
    params={'page_size':300,'status':'active'},timeout=60).json()
users={u['email']:u['id'] for u in r.get('users',[])}
jobs=[]
for w in work:
    uid=users.get(w['host'])
    if not uid: continue
    rr=requests.get(f'https://api.zoom.us/v2/users/{uid}/recordings',
        headers={'Authorization':f'Bearer {ztoken()}'},
        params={'from':w['date'],'to':w['date'],'page_size':300},timeout=60)
    if rr.status_code!=200: continue
    mt=next((x for x in rr.json().get('meetings',[]) if x['uuid']==w['uuid']),None)
    if not mt: continue
    ts=(w['start_time'] or '').replace('T',' ').replace('Z','')
    hf=get_folder(sanitize(w['host']),ZROOT)
    mf=get_folder(sanitize(f"{ts}: {w['topic']}"),hf)
    present=existing_names(mf)
    for f in mt.get('recording_files',[]):
        if f.get('status')=='completed':
            jobs.append((f,mf,ts,w['topic'],present))
print(f"{len(jobs)} files queued from {len(work)} meetings, {WORKERS} workers",flush=True)
t0=time.time()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(do_file,jobs))
el=time.time()-t0
print(f"\nDONE. files={stats['files']} skip={stats['skip']} fail={stats['fail']} "
      f"{stats['bytes']/1e9:.2f}GB in {el/60:.1f}min  AGGREGATE={stats['bytes']/1e6/max(el,1):.1f} MB/s")
