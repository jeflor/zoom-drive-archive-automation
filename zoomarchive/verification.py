"""The backup-verification gate.

The guiding rule of this project: never delete from Zoom without proving the
backup exists in Drive. That proof is a byte-size match, and this module is
where the proof is evaluated.

Why byte sizes and not MD5s: Zoom does not expose a checksum for cloud
recording files, so the strongest signal available before download is the
exact byte length Zoom reports, compared against the true byte length Drive
reports back. For multi-hundred-megabyte media an accidental collision is
vanishingly unlikely, and a meeting is only cleared when *every* one of its
media files matches.

An earlier cleanup verified against the Google Drive Desktop local cache
instead. That cache reports partially-synced files at the wrong size, which
produced false "corrupt backup" verdicts. All verification here reads sizes
from the Drive API.
"""
from collections import Counter

# Files below this size are metadata (chat, transcript, timeline, cc), not
# media. Zoom's reported size for them is unreliable and they collide on size
# constantly, so they are excluded from the strict gate — and never deleted.
MIN_MEDIA_BYTES = 1_000_000

SAFE_TO_DELETE = 'SAFE_TO_DELETE'
PARTIAL = 'PARTIAL'
NOT_BACKED_UP = 'NOT_BACKED_UP'
NO_MEDIA = 'NO_MEDIA'

CLASSIFICATION_ORDER = (SAFE_TO_DELETE, PARTIAL, NOT_BACKED_UP, NO_MEDIA)


def is_media(file_row) -> bool:
    """True if this recording file is media subject to the strict size gate."""
    return (file_row.get('size') or 0) >= MIN_MEDIA_BYTES


def drive_size_multiset(drive_rows) -> Counter:
    """Count how many Drive files exist at each exact byte size.

    A multiset, not a set: if a meeting has two distinct media files that
    happen to be the same length, the archive must contain *two* files of that
    length for the meeting to count as backed up. Using a plain set here would
    clear a meeting whose second file was never uploaded.
    """
    return Counter(r['size'] for r in drive_rows if is_media(r))


def classify_meeting(meeting, drive_sizes: Counter):
    """Classify one Zoom meeting against the Drive index.

    Returns (classification, matched_count, missing_count).

    `drive_sizes` is treated as read-only; matches are consumed from a local
    copy so that duplicate sizes within a single meeting each need their own
    counterpart in Drive.
    """
    media = [f for f in meeting['files'] if is_media(f)]
    if not media:
        # Transcripts/chat only. Nothing to protect and nothing to reclaim —
        # never classified as deletable.
        return NO_MEDIA, 0, 0

    available = Counter(drive_sizes)
    matched = missing = 0
    for f in media:
        if available[f['size']] > 0:
            available[f['size']] -= 1
            matched += 1
        else:
            missing += 1

    if missing == 0:
        return SAFE_TO_DELETE, matched, missing
    if matched == 0:
        return NOT_BACKED_UP, matched, missing
    # The dangerous middle: some media archived, some not. Never auto-deleted.
    return PARTIAL, matched, missing


def reconcile(zoom_meetings, drive_rows):
    """Classify every meeting. Read-only; returns one row per meeting."""
    drive_sizes = drive_size_multiset(drive_rows)
    rows = []
    for m in zoom_meetings:
        cls, matched, missing = classify_meeting(m, drive_sizes)
        media = [f for f in m['files'] if is_media(f)]
        size = m.get('total_size') or sum(f['size'] for f in m['files'])
        rows.append({
            'classification': cls,
            'host': m['host'],
            'start_time': m['start_time'],
            'topic': m['topic'][:60],
            'size_gb': round(size / 1e9, 2),
            'files_total': len(media),
            'files_matched': matched,
            'files_missing': missing,
            'uuid': m['uuid'],
            'meeting_id': m['meeting_id'],
        })
    return rows


def is_upload_verified(zoom_reported_size, drive_reported_size, file_type) -> bool:
    """Whether a just-completed upload may be recorded as a good backup.

    Strict byte equality for media. Zoom's size field for metadata files
    (summary/timeline) is unreliable, so those pass on existence alone —
    they are additive to the archive and are never used to gate a deletion.
    """
    if file_type in ('MP4', 'M4A'):
        return (drive_reported_size is not None
                and int(drive_reported_size) == int(zoom_reported_size))
    return drive_reported_size is not None
