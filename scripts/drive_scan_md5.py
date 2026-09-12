"""Re-walk the zBackup tree capturing md5Checksum for true duplicate detection.
Read-only. Writes drive_index_md5.json."""
import os
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
FOLDER='application/vnd.google-apps.folder'
creds=Credentials.from_authorized_user_file(os.environ.get('DRIVE_TOKEN_JSON', 'token.json'),['https://www.googleapis.com/auth/drive'])
if not creds.valid: creds.refresh(Request())
svc=build('drive','v3',credentials=creds,cache_discovery=False)
files=[]; fc=[0]
def walk(fid,path):
    tok=None
    while True:
        r=svc.files().list(q=f"'{fid}' in parents and trashed=false",
            fields='nextPageToken, files(id,name,size,md5Checksum,mimeType)',
            pageToken=tok,pageSize=1000,supportsAllDrives=True,includeItemsFromAllDrives=True,corpora='allDrives').execute()
        for it in r.get('files',[]):
            if it['mimeType']==FOLDER:
                fc[0]+=1
                if fc[0]%300==0: print(f"  ...{fc[0]} folders, {len(files)} files",flush=True)
                walk(it['id'],f"{path}/{it['name']}")
            else:
                files.append({'id':it['id'],'name':it['name'],'path':path,
                    'size':int(it['size']) if it.get('size') else 0,
                    'md5':it.get('md5Checksum')})
        tok=r.get('nextPageToken')
        if not tok: break
print("Walking zBackup tree with md5...",flush=True)
walk(ROOT,'')
json.dump(files,open('drive_index_md5.json','w'))
print(f"Done. {len(files)} files, {sum(1 for f in files if f['md5'])} with md5.")
