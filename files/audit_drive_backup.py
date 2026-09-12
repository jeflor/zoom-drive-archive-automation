#!/usr/bin/env python3
"""
Audits an existing Google Drive folder tree (e.g. the old zBackup destination)
and builds a manifest of every file already saved there.

This is a READ-ONLY scan. It does not touch Zoom and does not modify Drive.
Run this once before the main sync job, so the sync job can skip anything
that's already backed up, even though the old backup uses a different
folder structure and naming convention than the new one.

Matching strategy: file size, not filename. A recording's exact byte size
is effectively a fingerprint. Two unrelated video/audio files matching to
the exact byte is astronomically unlikely, so size is a reliable way to
detect "this recording already exists somewhere" regardless of how the
old system named or organized it.

Usage:
    python audit_drive_backup.py --root-folder-id FOLDER_ID --output manifest.json

Optional:
    --upload-to-bucket BUCKET_NAME   # also uploads manifest.json to GCS
    --blob-name PATH_IN_BUCKET       # defaults to zoom-backup/drive_manifest.json
"""

import argparse
import json

import google.auth
from googleapiclient.discovery import build


def get_drive_service():
    creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def walk_folder(drive_service, folder_id, path, manifest, folder_count, file_count):
    """Recursively walk a Drive folder tree, recording every file's size."""
    page_token = None
    while True:
        response = drive_service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id, name, size, mimeType, modifiedTime)',
            pageToken=page_token,
            pageSize=1000,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora='allDrives'
        ).execute()

        for item in response.get('files', []):
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                folder_count[0] += 1
                walk_folder(
                    drive_service, item['id'], f"{path}/{item['name']}",
                    manifest, folder_count, file_count
                )
            else:
                size = int(item.get('size', 0)) if item.get('size') else 0
                file_count[0] += 1
                manifest.append({
                    'name': item['name'],
                    'size': size,
                    'modifiedTime': item.get('modifiedTime'),
                    'path': path,
                })

        page_token = response.get('nextPageToken')
        if not page_token:
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root-folder-id', required=True, help='Drive folder ID to scan (e.g. the old zBackup root)')
    parser.add_argument('--output', default='manifest.json', help='Local file to write the manifest to')
    parser.add_argument('--upload-to-bucket', help='Optional: GCS bucket name to also upload the manifest to')
    parser.add_argument('--blob-name', default='zoom-backup/drive_manifest.json')
    args = parser.parse_args()

    drive_service = get_drive_service()
    manifest = []
    folder_count = [0]
    file_count = [0]

    print(f"Scanning Drive folder tree starting at {args.root_folder_id}...")
    walk_folder(drive_service, args.root_folder_id, '', manifest, folder_count, file_count)

    print(f"Found {file_count[0]} files across {folder_count[0]} folders.")

    with open(args.output, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"Manifest written to {args.output}")

    total_bytes = sum(item['size'] for item in manifest)
    print(f"Total size scanned: {total_bytes / (1024**3):.2f} GB")

    if args.upload_to_bucket:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(args.upload_to_bucket)
        blob = bucket.blob(args.blob_name)
        blob.upload_from_filename(args.output, content_type='application/json')
        print(f"Manifest also uploaded to gs://{args.upload_to_bucket}/{args.blob_name}")


if __name__ == '__main__':
    main()
