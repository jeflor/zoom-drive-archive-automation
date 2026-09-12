"""Read-only recursive scan of the real zBackup tree in Drive.
Records every file's true byte size (read back from the Drive API, not a
local cache). Writes drive_index.json. Safe: never writes to Drive."""
import os
import json
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

OUT = Path(__file__).parent / 'drive_index.json'
ZBACKUP_ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
FOLDER_MIME = 'application/vnd.google-apps.folder'

creds = Credentials.from_authorized_user_file(
    os.environ.get('DRIVE_TOKEN_JSON', 'token.json'),
    ['https://www.googleapis.com/auth/drive'])
if not creds.valid:
    creds.refresh(Request())
svc = build('drive', 'v3', credentials=creds, cache_discovery=False)

files = []            # {name, size, path, id}
folder_count = [0]


def walk(fid, path):
    tok = None
    while True:
        r = svc.files().list(
            q=f"'{fid}' in parents and trashed=false",
            fields='nextPageToken, files(id,name,size,mimeType)',
            pageToken=tok, pageSize=1000,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
        ).execute()
        for it in r.get('files', []):
            if it['mimeType'] == FOLDER_MIME:
                folder_count[0] += 1
                if folder_count[0] % 200 == 0:
                    print(f"  ...{folder_count[0]} folders, {len(files)} files so far", flush=True)
                walk(it['id'], f"{path}/{it['name']}")
            else:
                files.append({
                    'name': it['name'],
                    'size': int(it['size']) if it.get('size') else 0,
                    'path': path,
                    'id': it['id'],
                })
        tok = r.get('nextPageToken')
        if not tok:
            break


print("Scanning zBackup tree (this walks ~2,700 folders)...", flush=True)
walk(ZBACKUP_ROOT, '')
print(f"Done. {len(files)} files across {folder_count[0]} folders.")

by_size = {}
for f in files:
    by_size.setdefault(f['size'], 0)
    by_size[f['size']] += 1

json.dump({'files': files, 'size_counts': by_size}, open(OUT, 'w'))
total = sum(f['size'] for f in files)
print(f"Total: {total/1e9:.1f} GB. Distinct sizes: {len(by_size)}. Wrote {OUT.name}")
