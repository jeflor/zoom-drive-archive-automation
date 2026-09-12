#!/usr/bin/env python3
"""
Generates the synthetic sample data under fixtures/, matching the exact schemas
the pipeline reads and writes.

The real archive's indexes are not in this repo: they describe private meetings
(titles, host accounts, Zoom UUIDs, Drive file ids). These fixtures are
deterministically generated stand-ins, so the shape of every file the tooling
consumes is visible and the test suite has something to run against.

    python3 fixtures/make_fixtures.py
"""
import base64
import hashlib
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
RNG = random.Random(20260913)  # deterministic output

HOSTS = ['archive@example.com', 'training@example.com', 'media@example.com']
TOPICS = [
    'Weekly Team Q&A',
    'Daily Live Training Session',
    '3-Day Virtual Workshop',
    'Member Onboarding Walkthrough',
    'Quarterly Strategy Review',
    'Guest Expert Interview',
    'Office Hours',
]
LAYOUTS = [
    'shared_screen_with_speaker_view',
    'shared_screen_with_gallery_view',
    'gallery_view',
    'active_speaker',
    'shared_screen',
]


def fake_uuid() -> str:
    """Zoom meeting UUIDs are base64 with padding, and may contain / and +."""
    return base64.b64encode(RNG.randbytes(16)).decode()


def fake_file_id() -> str:
    """Zoom recording-file ids are lowercase UUID4-shaped."""
    h = RNG.randbytes(16).hex()
    return f'{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}'


def fake_drive_id() -> str:
    """Drive file ids: 33 chars, leading '1'."""
    alpha = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'
    return '1' + ''.join(RNG.choice(alpha) for _ in range(32))


def meeting(n: int) -> dict:
    host = RNG.choice(HOSTS)
    topic = RNG.choice(TOPICS)
    month, day = RNG.randint(1, 12), RNG.randint(1, 28)
    start = f'2026-{month:02d}-{day:02d}T{RNG.randint(13, 21):02d}:{RNG.randint(0, 59):02d}:{RNG.randint(0, 59):02d}Z'

    files = []
    # Zoom renders several video layouts per meeting — this duplication is the
    # whole reason the archive was 4.5x larger than the billed figure.
    for layout in RNG.sample(LAYOUTS, RNG.randint(2, 4)):
        files.append({'id': fake_file_id(), 'file_type': 'MP4',
                      'recording_type': layout,
                      'size': RNG.randint(180_000_000, 2_400_000_000)})
    files.append({'id': fake_file_id(), 'file_type': 'M4A',
                  'recording_type': 'audio_only',
                  'size': RNG.randint(18_000_000, 90_000_000)})
    # Metadata files: tiny, and Zoom's reported size for them is unreliable.
    for ft in ('TRANSCRIPT', 'CHAT', 'TIMELINE'):
        if RNG.random() < 0.6:
            files.append({'id': fake_file_id(), 'file_type': ft,
                          'recording_type': ft.lower(),
                          'size': RNG.randint(700, 240_000)})
    return {'host': host, 'uuid': fake_uuid(),
            'meeting_id': RNG.randint(80_000_000_000, 89_999_999_999),
            'topic': topic, 'start_time': start,
            'total_size': sum(f['size'] for f in files), 'files': files}


def write(name: str, obj) -> None:
    path = os.path.join(HERE, name)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=1)
        fh.write('\n')
    print(f'{name:34} {os.path.getsize(path):>8,} bytes')


def main() -> None:
    meetings = [meeting(i) for i in range(40)]
    write('zoom_recordings.json', meetings)

    # Drive index: the archive tree, one row per file, with MD5s for dedup.
    # Deliberately seed a few duplicate MD5s — the real archive was full of them.
    drive, dupe_pool = [], []
    for m in meetings[:32]:
        folder = f"/{m['host']}/{m['start_time'][:10].replace('-', '_')}T" \
                 f"{m['start_time'][11:16].replace(':', '-')} {m['topic'].replace(' ', ' ')}"
        for f in m['files']:
            if f['file_type'] == 'MP4':
                ext, suffix = 'mp4', f" ({f['recording_type']})"
            elif f['file_type'] == 'M4A':
                ext, suffix = 'm4a', ''
            else:
                ext, suffix = 'txt', f" ({f['file_type'].lower()})"
            md5 = hashlib.md5(f['id'].encode()).hexdigest()
            row = {'id': fake_drive_id(),
                   'name': f"{m['start_time'][:10]} {m['topic']}{suffix}.{ext}",
                   'path': folder, 'size': f['size'], 'md5': md5}
            drive.append(row)
            if f['file_type'] == 'MP4' and RNG.random() < 0.25:
                dupe_pool.append(row)
    # Redundant second copies under a variant folder name.
    for row in dupe_pool:
        drive.append({**row, 'id': fake_drive_id(),
                      'path': row['path'] + ' (1)'})
    write('drive_index_md5.json', drive)

    # Reconciliation output: the classification that gates every deletion.
    recon = []
    for i, m in enumerate(meetings):
        media = [f for f in m['files']
                 if f['file_type'] in ('MP4', 'M4A') and f['size'] >= 1_000_000]
        total = len(media)
        if i < 32:
            matched, cls = total, 'SAFE_TO_DELETE'
        elif i < 36:
            matched, cls = max(total - 1, 0), 'PARTIAL'
        else:
            matched, cls = 0, 'NOT_BACKED_UP'
        if total == 0:
            matched, cls = 0, 'NO_MEDIA'
        recon.append({'classification': cls, 'host': m['host'],
                      'start_time': m['start_time'], 'topic': m['topic'],
                      'size_gb': round(m['total_size'] / 1e9, 2),
                      'files_total': total, 'files_matched': matched,
                      'files_missing': total - matched,
                      'uuid': m['uuid'], 'meeting_id': m['meeting_id']})
    write('reconciliation.json', recon)

    # Worklist: what the cloud backfill job still has to move.
    worklist = [{'uuid': m['uuid'], 'meeting_id': m['meeting_id'],
                 'host': m['host'], 'date': m['start_time'][:10],
                 'start_time': m['start_time'], 'topic': m['topic'],
                 'missing_media': [{k: f[k] for k in
                                    ('id', 'recording_type', 'file_type', 'size')}
                                   for f in m['files']
                                   if f['file_type'] in ('MP4', 'M4A')][:2]}
                for m in meetings[36:]]
    write('backup_worklist.json', worklist)

    # Delete batch: Drive-verified, so safe to trash in Zoom.
    batch = [{'uuid': m['uuid'], 'host': m['host'], 'date': m['start_time'][:10],
              'topic': m['topic'],
              'drive_folder': f"/{m['host']}/{m['start_time'][:10]} {m['topic']}",
              'files': [{k: f[k] for k in
                         ('id', 'recording_type', 'file_type', 'size')}
                        for f in m['files'] if f['file_type'] in ('MP4', 'M4A')]}
             for m in meetings[:12]]
    write('batch_delete.json', batch)

    # Meetings the per-file delete API refused with error 3332.
    write('ra_locked.json',
          [{'date': m['start_time'][:10], 'host': m['host'], 'topic': m['topic'],
            'gb': round(m['total_size'] / 1e9, 2), 'uuid': m['uuid']}
           for m in meetings[:9]])

    write('dedupe_delete_ids.json', [r['id'] for r in drive[-len(dupe_pool):]])


if __name__ == '__main__':
    main()
