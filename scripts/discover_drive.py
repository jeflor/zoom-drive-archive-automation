#!/usr/bin/env python3
"""
Read-only Drive reconnaissance, run locally with the existing token.json.

Lists every shared drive the account can see, plus the top-level folders in
each, plus the top-level folders in My Drive. Use it to find the folder ID of
the old zBackup destination, which is the one thing blocking the audit step.

Nothing here writes to Drive or touches Zoom.

    ./venv/bin/python discover_drive.py
"""

from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

HERE = Path(__file__).parent
SCOPES = ['https://www.googleapis.com/auth/drive']

FOLDER_MIME = 'application/vnd.google-apps.folder'


def get_service():
    token_file = HERE / 'token.json'
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def list_children(service, parent_id, folders_only=True):
    q = f"'{parent_id}' in parents and trashed=false"
    if folders_only:
        q += f" and mimeType='{FOLDER_MIME}'"
    out = []
    page_token = None
    while True:
        resp = service.files().list(
            q=q, fields='nextPageToken, files(id, name, mimeType)',
            pageToken=page_token, pageSize=200,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives',
        ).execute()
        out.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


def describe(service, folder_id, label):
    """Print a folder's immediate subfolders so it can be identified by eye."""
    try:
        subs = list_children(service, folder_id)
    except Exception as e:
        print(f"    (could not list: {e})")
        return
    print(f"    {len(subs)} subfolders{':' if subs else ''}")
    for s in subs[:15]:
        print(f"      - {s['name']}  [{s['id']}]")
    if len(subs) > 15:
        print(f"      ... and {len(subs) - 15} more")


def main():
    service = get_service()

    about = service.about().get(fields='user(emailAddress)').execute()
    print(f"Authenticated as: {about['user']['emailAddress']}\n")

    print("=== Shared drives ===")
    drives = service.drives().list(pageSize=100, fields='drives(id, name)').execute().get('drives', [])
    if not drives:
        print("  (none visible to this account)")
    for d in drives:
        print(f"\n  {d['name']}  [{d['id']}]")
        describe(service, d['id'], d['name'])

    print("\n=== My Drive, top-level folders ===")
    for f in list_children(service, 'root'):
        print(f"  {f['name']}  [{f['id']}]")

    print("\n=== Folders shared with me ===")
    resp = service.files().list(
        q=f"sharedWithMe=true and mimeType='{FOLDER_MIME}' and trashed=false",
        fields='files(id, name)', pageSize=100,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    for f in resp.get('files', []):
        print(f"  {f['name']}  [{f['id']}]")


if __name__ == '__main__':
    main()
