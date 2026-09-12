#!/usr/bin/env python3
"""
Template for config.py, which holds shared credentials and is imported by the
tooling in scripts/ (they do `from config import CONFIG` rather than
restating credentials).

The real config.py is gitignored because it contains live secrets.
Copy this file to config.py and fill in the values, or better, export the
environment variables and leave the os.environ lookups in place.

    export ZOOM_ACCOUNT_ID=...
    export ZOOM_CLIENT_ID=...
    export ZOOM_CLIENT_SECRET=...

Zoom app scopes required (Server-to-Server OAuth app):
    cloud_recording:read:list_user_recordings:admin    # list recordings
    cloud_recording:delete:recording_file:admin        # trash media files
    cloud_recording:read:list_recording_files:admin    # per-meeting files
                                                       # (needed only for
                                                       #  per-participant audio)
"""

import os

CONFIG = {
    'ZOOM_ACCOUNT_ID': os.environ.get('ZOOM_ACCOUNT_ID', 'YOUR_ACCOUNT_ID'),
    'ZOOM_CLIENT_ID': os.environ.get('ZOOM_CLIENT_ID', 'YOUR_CLIENT_ID'),
    'ZOOM_CLIENT_SECRET': os.environ.get('ZOOM_CLIENT_SECRET', 'YOUR_CLIENT_SECRET'),

    # Drive folder that holds the backup tree (zBackup root)
    'DRIVE_FOLDER_ID': os.environ.get('DRIVE_ROOT_FOLDER_ID', 'YOUR_DRIVE_FOLDER_ID'),

    'FROM_DATE': '2023-01-01',
    'TO_DATE': None,  # None = today
    'DOWNLOAD_TYPES': ['MP4', 'M4A', 'CHAT', 'TRANSCRIPT', 'TIMELINE', 'CC'],
}

# Google Drive auth: the tooling authenticates as a user (not a service
# account) via an OAuth refresh token stored in token.json, which is also
# gitignored. token.json is produced by the standard InstalledAppFlow using
# credentials.json. Keep both out of version control.
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
