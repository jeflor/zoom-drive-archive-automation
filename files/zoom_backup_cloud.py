#!/usr/bin/env python3
"""
Zoom cloud recording backup, cloud-native version.

Runs as a scheduled Cloud Run Job (triggered by Cloud Scheduler). No local
external drive, no manual phases. Each recording is streamed from Zoom into
a per-user, per-meeting folder on a shared Google Drive.

State (which files have already been synced) is kept in a small JSON blob
in Cloud Storage, so it survives between runs instead of living on disk.

Required environment variables:
    ZOOM_ACCOUNT_ID
    ZOOM_CLIENT_ID
    ZOOM_CLIENT_SECRET
    DRIVE_ROOT_FOLDER_ID       # folder inside your shared Drive where
                               # per-user subfolders get created
    STATE_BUCKET               # GCS bucket name for state.json
    GOOGLE_APPLICATION_CREDENTIALS
                               # path to a service account key file,
                               # mounted as a secret in Cloud Run.
                               # That service account must be added as a
                               # member (Content Manager or higher) on the
                               # shared Drive.

Optional environment variables:
    FROM_DATE                  # YYYY-MM-DD, defaults to 7 days ago
    TO_DATE                    # YYYY-MM-DD, defaults to today
    SKIP_KEYWORDS              # comma-separated, e.g. "1-Day,Test"
    STATE_BLOB_NAME             # defaults to "zoom-backup/state.json"
    DRIVE_MANIFEST_BLOB_NAME    # defaults to "zoom-backup/drive_manifest.json"
                               # produced by audit_drive_backup.py. If present,
                               # recordings already sitting in the old backup
                               # (matched by exact byte size) are skipped
                               # instead of re-downloaded.
    DRY_RUN                    # "true" to log what would happen without
                               # downloading or uploading anything
"""

import os
import io
import re
import json
import base64
import tempfile
from datetime import datetime, timedelta

import requests
import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.cloud import storage

# ============ CONFIG ============
ZOOM_ACCOUNT_ID = os.environ['ZOOM_ACCOUNT_ID']
ZOOM_CLIENT_ID = os.environ['ZOOM_CLIENT_ID']
ZOOM_CLIENT_SECRET = os.environ['ZOOM_CLIENT_SECRET']

DRIVE_ROOT_FOLDER_ID = os.environ['DRIVE_ROOT_FOLDER_ID']

STATE_BUCKET = os.environ['STATE_BUCKET']
STATE_BLOB_NAME = os.environ.get('STATE_BLOB_NAME', 'zoom-backup/state.json')
DRIVE_MANIFEST_BLOB_NAME = os.environ.get('DRIVE_MANIFEST_BLOB_NAME', 'zoom-backup/drive_manifest.json')

DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'

# Below this size (bytes), a match is only trusted if it's also close in time
# to the meeting, since small chat/timeline files are more likely to collide
# on exact byte size by coincidence than large video/audio files are.
SMALL_FILE_THRESHOLD = 1 * 1024 * 1024  # 1 MB
SMALL_FILE_DATE_WINDOW_DAYS = 30

FROM_DATE = os.environ.get(
    'FROM_DATE', (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
)
TO_DATE = os.environ.get('TO_DATE', datetime.now().strftime('%Y-%m-%d'))

SKIP_KEYWORDS = [
    kw.strip() for kw in os.environ.get('SKIP_KEYWORDS', '1-Day').split(',') if kw.strip()
]

DOWNLOAD_TYPES = ['MP4', 'M4A', 'CHAT', 'TRANSCRIPT', 'TIMELINE', 'CC']

DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
# ================================


# ---------- State (Cloud Storage instead of local file) ----------

def load_state():
    client = storage.Client()
    bucket = client.bucket(STATE_BUCKET)
    blob = bucket.blob(STATE_BLOB_NAME)
    if blob.exists():
        return json.loads(blob.download_as_text())
    return {'synced_file_ids': [], 'user_folder_ids': {}, 'meeting_folder_ids': {}}


def save_state(state):
    client = storage.Client()
    bucket = client.bucket(STATE_BUCKET)
    blob = bucket.blob(STATE_BLOB_NAME)
    blob.upload_from_string(json.dumps(state, indent=2), content_type='application/json')


def load_drive_manifest():
    """Loads the manifest produced by audit_drive_backup.py, if one exists.
    Returns a dict mapping size -> list of {name, modifiedTime, path} entries,
    since multiple existing files can share the same size."""
    client = storage.Client()
    bucket = client.bucket(STATE_BUCKET)
    blob = bucket.blob(DRIVE_MANIFEST_BLOB_NAME)
    if not blob.exists():
        print("No Drive manifest found, treating everything as not-yet-backed-up.")
        return {}

    entries = json.loads(blob.download_as_text())
    by_size = {}
    for entry in entries:
        by_size.setdefault(entry['size'], []).append(entry)
    print(f"Loaded Drive manifest: {len(entries)} existing files across {len(by_size)} distinct sizes.")
    return by_size


def already_backed_up(manifest_by_size, file_size, meeting_start_time):
    """Checks whether a Zoom recording file already exists somewhere in the
    old backup, using exact byte size as the fingerprint. For small files,
    also requires the match to be within a plausible time window, since tiny
    files are more likely to collide on size by coincidence."""
    candidates = manifest_by_size.get(file_size)
    if not candidates:
        return False

    if file_size >= SMALL_FILE_THRESHOLD:
        return True

    try:
        meeting_dt = datetime.strptime(meeting_start_time[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return True  # can't check date, trust the size match

    for candidate in candidates:
        modified = candidate.get('modifiedTime')
        if not modified:
            continue
        try:
            modified_dt = datetime.strptime(modified[:10], '%Y-%m-%d')
        except ValueError:
            continue
        if abs((modified_dt - meeting_dt).days) <= SMALL_FILE_DATE_WINDOW_DAYS:
            return True
    return False


# ---------- Zoom ----------

def get_zoom_token():
    creds = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        'https://zoom.us/oauth/token',
        params={'grant_type': 'account_credentials', 'account_id': ZOOM_ACCOUNT_ID},
        headers={'Authorization': f'Basic {creds}'}
    )
    r.raise_for_status()
    return r.json()['access_token']


def zoom_get(url, token, params=None):
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, params=params)
    r.raise_for_status()
    return r.json()


def fetch_all_users(token):
    users = []
    next_page = ''
    while True:
        params = {'page_size': 300, 'status': 'active'}
        if next_page:
            params['next_page_token'] = next_page
        data = zoom_get('https://api.zoom.us/v2/users', token, params)
        users.extend(data.get('users', []))
        next_page = data.get('next_page_token', '')
        if not next_page:
            break
    return users


def fetch_user_recordings(user_id, from_date, to_date, token):
    meetings = []
    current = datetime.strptime(from_date, '%Y-%m-%d')
    end = datetime.strptime(to_date, '%Y-%m-%d')

    while current <= end:
        chunk_end = min(current + timedelta(days=30), end)
        next_page = ''
        while True:
            params = {
                'from': current.strftime('%Y-%m-%d'),
                'to': chunk_end.strftime('%Y-%m-%d'),
                'page_size': 300
            }
            if next_page:
                params['next_page_token'] = next_page
            data = zoom_get(f'https://api.zoom.us/v2/users/{user_id}/recordings', token, params)
            meetings.extend(data.get('meetings', []))
            next_page = data.get('next_page_token', '')
            if not next_page:
                break
        current = chunk_end + timedelta(days=1)
    return meetings


# ---------- Helpers ----------

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name)[:100]


def get_extension(file_type, file_extension):
    if file_extension:
        return file_extension.lower().lstrip('.')
    return {
        'MP4': 'mp4', 'M4A': 'm4a', 'CHAT': 'txt',
        'TRANSCRIPT': 'vtt', 'TIMELINE': 'json', 'CC': 'vtt'
    }.get(file_type, 'bin')


def should_skip(topic):
    lower = topic.lower()
    return any(kw.lower() in lower for kw in SKIP_KEYWORDS)


# ---------- Drive ----------

def get_drive_service():
    creds, _ = google.auth.default(scopes=DRIVE_SCOPES)
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def get_or_create_subfolder(drive_service, name, parent_id, cache, cache_key):
    """Look up a folder by name under parent_id, creating it if missing.
    Uses a cache dict (backed by state) to avoid repeat lookups on every run."""
    if cache_key in cache:
        return cache[cache_key]

    safe_name = name.replace("'", "\\'")
    query = (
        f"name='{safe_name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    existing = drive_service.files().list(
        q=query, fields='files(id)',
        supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute().get('files', [])

    if existing:
        folder_id = existing[0]['id']
    else:
        meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        folder_id = drive_service.files().create(
            body=meta, fields='id', supportsAllDrives=True
        ).execute()['id']

    cache[cache_key] = folder_id
    return folder_id


def file_already_in_drive(drive_service, filename, parent_id):
    safe_name = filename.replace("'", "\\'")
    query = f"name='{safe_name}' and '{parent_id}' in parents and trashed=false"
    existing = drive_service.files().list(
        q=query, fields='files(id)',
        supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
    ).execute().get('files', [])
    return bool(existing)


def stream_to_drive(download_url, zoom_token, filename, mimetype, drive_service, parent_id):
    """Download one Zoom recording file to a temp file in /tmp, then upload it
    to Drive with a resumable session, then remove the temp file. /tmp on
    Cloud Run is memory-backed and wiped when the job ends, it is not a
    persistent external drive."""
    with requests.get(
        download_url, headers={'Authorization': f'Bearer {zoom_token}'}, stream=True
    ) as r:
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            for chunk in r.iter_content(chunk_size=8192 * 16):
                if chunk:
                    tmp.write(chunk)
            tmp_path = tmp.name

    try:
        media = MediaFileUpload(tmp_path, mimetype=mimetype, resumable=True)
        drive_service.files().create(
            body={'name': filename, 'parents': [parent_id]},
            media_body=media, fields='id', supportsAllDrives=True
        ).execute()
    finally:
        os.remove(tmp_path)


# ---------- Main sync logic ----------

def process_meeting(meeting, user_email, zoom_token, drive_service, state, manifest_by_size, stats):
    date = meeting['start_time'][:10]
    topic = sanitize(meeting.get('topic', 'Untitled'))
    meeting_uuid = meeting['uuid']

    for f in meeting.get('recording_files', []):
        if f.get('file_type') not in DOWNLOAD_TYPES:
            continue
        if f.get('status') != 'completed':
            continue

        file_id = f['id']
        if file_id in state['synced_file_ids']:
            stats['already_known'] += 1
            continue

        ext = get_extension(f['file_type'], f.get('file_extension', ''))
        rec_type = f.get('recording_type', f['file_type'])
        filename = f"{date} - {topic} - {rec_type}.{ext}"
        file_size = f.get('file_size', 0)

        # Gap-detection: is this recording already sitting in the old
        # (differently organized) backup? Checked by exact byte size.
        if already_backed_up(manifest_by_size, file_size, meeting['start_time']):
            print(f"  [already in old backup] {user_email} / {date} - {topic} / {filename}")
            state['synced_file_ids'].append(file_id)
            stats['found_in_old_backup'] += 1
            if not DRY_RUN:
                save_state(state)
            continue

        stats['needs_sync'] += 1
        if DRY_RUN:
            print(f"  [DRY RUN would sync] {user_email} / {date} - {topic} / {filename} ({file_size/1024/1024:.1f} MB)")
            continue

        # Only create folders and touch Drive once we know we're actually
        # about to sync something, so dry runs don't create empty folders.
        user_folder_id = get_or_create_subfolder(
            drive_service, sanitize(user_email), DRIVE_ROOT_FOLDER_ID,
            state['user_folder_ids'], user_email
        )
        meeting_folder_name = f"{date} - {topic}"
        meeting_folder_id = get_or_create_subfolder(
            drive_service, meeting_folder_name, user_folder_id,
            state['meeting_folder_ids'], meeting_uuid
        )

        if file_already_in_drive(drive_service, filename, meeting_folder_id):
            state['synced_file_ids'].append(file_id)
            save_state(state)
            continue

        download_url = f['download_url'] + '?access_token=' + zoom_token
        print(f"  Syncing: {user_email} / {meeting_folder_name} / {filename}")
        try:
            stream_to_drive(
                download_url, zoom_token, filename,
                'application/octet-stream', drive_service, meeting_folder_id
            )
            state['synced_file_ids'].append(file_id)
            save_state(state)
        except Exception as e:
            print(f"    ERROR: {e}")


def main():
    if DRY_RUN:
        print("=== DRY RUN: no files will be downloaded or uploaded ===")

    state = load_state()
    manifest_by_size = load_drive_manifest()

    print("Getting Zoom token...")
    token = get_zoom_token()

    print("Fetching user list...")
    users = fetch_all_users(token)
    print(f"Found {len(users)} users.")

    drive_service = get_drive_service()

    meeting_count = 0
    stats = {'already_known': 0, 'found_in_old_backup': 0, 'needs_sync': 0}
    for user in users:
        print(f"Fetching recordings for {user['email']}...")
        meetings = fetch_user_recordings(user['id'], FROM_DATE, TO_DATE, token)

        for meeting in meetings:
            meeting_count += 1
            topic = meeting.get('topic', 'Untitled')
            if should_skip(topic):
                continue
            if meeting_count % 30 == 0:
                token = get_zoom_token()  # Zoom tokens last 1 hour
            try:
                process_meeting(meeting, user['email'], token, drive_service, state, manifest_by_size, stats)
            except Exception as e:
                print(f"  ERROR on meeting {meeting.get('uuid')}: {e}")

    print(f"\nDone. Checked {meeting_count} meetings across {len(users)} users.")
    print(f"  Already synced in a prior run: {stats['already_known']}")
    print(f"  Found in old backup (skipped): {stats['found_in_old_backup']}")
    print(f"  {'Would need syncing' if DRY_RUN else 'Synced'}: {stats['needs_sync']}")


if __name__ == '__main__':
    main()
