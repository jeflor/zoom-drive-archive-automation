import os
import sys, io, json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from collections import Counter
import openpyxl

ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']

# my 1542 list
confirmed = json.load(open('reconciliation.json'))  # fallback not used
# rebuild from verify output by re-reading the xlsx we generated
wb_mine = openpyxl.load_workbook('safe_to_delete.xlsx', read_only=True, data_only=True)
ws = wb_mine['Confirmed Safe (media)']
hdr = list(next(ws.iter_rows(max_row=1, values_only=True)))
iu = hdr.index('uuid')
mine = set()
mine_rows = {}
for row in ws.iter_rows(min_row=2, values_only=True):
    if row and row[iu]:
        mine.add(row[iu]); mine_rows[row[iu]] = row
print(f"My confirmed-safe list: {len(mine)} meetings (unique UUIDs: {len(mine)})")

# load the 927 deletion sheet
creds = Credentials.from_authorized_user_file(os.environ.get('DRIVE_TOKEN_JSON', 'token.json'), ['https://www.googleapis.com/auth/drive'])
if not creds.valid: creds.refresh(Request())
svc = build('drive','v3',credentials=creds,cache_discovery=False)
r=svc.files().list(q=f"'{ROOT}' in parents and name='prior_audit.xlsx' and trashed=false",
    fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives').execute()
buf=io.BytesIO(); dl=MediaIoBaseDownload(buf, svc.files().get_media(fileId=r['files'][0]['id'], supportsAllDrives=True))
done=False
while not done: _,done=dl.next_chunk()
buf.seek(0); wb=openpyxl.load_workbook(buf, read_only=True, data_only=True)
ws2=wb['Deletion Candidates (2TB)']
h2=list(next(ws2.iter_rows(max_row=1, values_only=True)))
iU=h2.index('Meeting UUID'); iD=h2.index('Deletion Status'); iV=h2.index('Verification Status')
deleted=set(); allcand=set(); status=Counter()
for row in ws2.iter_rows(min_row=2, values_only=True):
    if not row or not row[iU]: continue
    allcand.add(row[iU]); status[str(row[iD])[:30]] += 1
    if 'ALREADY DELETED' in str(row[iD]): deleted.add(row[iU])
print(f"927 sheet: {len(allcand)} candidate UUIDs; deletion-status tally: {dict(status)}")
print(f"  of which ALREADY DELETED: {len(deleted)}")

ov_all = mine & allcand
ov_del = mine & deleted
print(f"\n=== OVERLAP ===")
print(f"My 1542 vs ALL 927 candidates : {len(ov_all)}")
print(f"My 1542 vs ALREADY-DELETED    : {len(ov_del)}")
if ov_all:
    print("\nOverlapping UUIDs (first 20):")
    for u in list(ov_all)[:20]:
        print(f"  {u}  ->  {mine_rows[u][hdr.index('date')]} {mine_rows[u][hdr.index('topic')]}")
else:
    print("\nNo overlap. None of the 1542 safe meetings are in the deletion sheet.")
