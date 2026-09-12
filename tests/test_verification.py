"""The deletion gate. Every test here guards against irreversible data loss."""
from conftest import drive, media, meeting

from zoomarchive.verification import (MIN_MEDIA_BYTES, NO_MEDIA, NOT_BACKED_UP,
                                      PARTIAL, SAFE_TO_DELETE,
                                      classify_meeting, drive_size_multiset,
                                      is_upload_verified, reconcile)


def sizes(*drive_rows):
    return drive_size_multiset(list(drive_rows))


def test_all_media_present_is_safe_to_delete():
    m = meeting(media(500_000_000), media(240_000_000))
    cls, matched, missing = classify_meeting(
        m, sizes(drive(500_000_000), drive(240_000_000)))
    assert (cls, matched, missing) == (SAFE_TO_DELETE, 2, 0)


def test_nothing_present_is_not_backed_up():
    m = meeting(media(500_000_000))
    assert classify_meeting(m, sizes())[0] == NOT_BACKED_UP


def test_some_media_missing_is_partial_never_safe():
    """The dangerous middle case: a partial backup must never read as safe."""
    m = meeting(media(500_000_000), media(240_000_000))
    cls, matched, missing = classify_meeting(m, sizes(drive(500_000_000)))
    assert cls == PARTIAL
    assert (matched, missing) == (1, 1)


def test_duplicate_sizes_need_duplicate_backups():
    """Two same-length media files require TWO archived copies, not one.

    This is why the Drive index is a multiset. With a plain set, the second
    file would match the first file's row and the meeting would be cleared for
    deletion while that file had never been uploaded.
    """
    m = meeting(media(500_000_000, id='a'), media(500_000_000, id='b'))

    one_copy = classify_meeting(m, sizes(drive(500_000_000, id='d1')))
    assert one_copy[0] == PARTIAL, 'one archived copy must not clear two files'

    two_copies = classify_meeting(
        m, sizes(drive(500_000_000, id='d1'), drive(500_000_000, id='d2')))
    assert two_copies[0] == SAFE_TO_DELETE


def test_metadata_only_meeting_is_never_deletable():
    """Transcript/chat-only meetings have nothing to reclaim, so never delete."""
    transcript = {'id': 't1', 'file_type': 'TRANSCRIPT',
                  'recording_type': 'transcript', 'size': 4_096}
    cls, matched, missing = classify_meeting(meeting(transcript), sizes())
    assert cls == NO_MEDIA
    assert cls != SAFE_TO_DELETE


def test_metadata_files_excluded_from_the_gate():
    """A meeting is cleared on its media alone; tiny files don't block it."""
    chat = {'id': 'c1', 'file_type': 'CHAT', 'recording_type': 'chat',
            'size': 900}
    m = meeting(media(500_000_000), chat)
    assert classify_meeting(m, sizes(drive(500_000_000)))[0] == SAFE_TO_DELETE


def test_media_threshold_boundary():
    below = meeting({'id': 'x', 'file_type': 'MP4',
                     'recording_type': 'active_speaker',
                     'size': MIN_MEDIA_BYTES - 1})
    assert classify_meeting(below, sizes())[0] == NO_MEDIA

    at = meeting(media(MIN_MEDIA_BYTES))
    assert classify_meeting(at, sizes())[0] == NOT_BACKED_UP


def test_classify_does_not_mutate_the_shared_index():
    """One meeting's matches must not consume another's."""
    index = sizes(drive(500_000_000))
    m = meeting(media(500_000_000))
    assert classify_meeting(m, index)[0] == SAFE_TO_DELETE
    assert classify_meeting(m, index)[0] == SAFE_TO_DELETE
    assert index[500_000_000] == 1


def test_reconcile_reports_every_meeting_once():
    zoom = [meeting(media(500_000_000), uuid='A=='),
            meeting(media(120_000_000), uuid='B==')]
    rows = reconcile(zoom, [drive(500_000_000)])
    assert [r['uuid'] for r in rows] == ['A==', 'B==']
    assert [r['classification'] for r in rows] == [SAFE_TO_DELETE,
                                                   NOT_BACKED_UP]


def test_reconcile_accepts_the_generated_fixtures():
    """Sanity-check the pipeline against fixtures/ end to end."""
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    fx = os.path.join(os.path.dirname(here), 'fixtures')
    with open(os.path.join(fx, 'zoom_recordings.json')) as fh:
        zoom = json.load(fh)
    with open(os.path.join(fx, 'drive_index_md5.json')) as fh:
        drive_rows = json.load(fh)
    rows = reconcile(zoom, drive_rows)
    assert len(rows) == len(zoom)
    assert any(r['classification'] == SAFE_TO_DELETE for r in rows)
    for r in rows:
        assert r['files_matched'] + r['files_missing'] == r['files_total']


class TestUploadVerification:
    """Post-upload byte check: what may be recorded as a good backup."""

    def test_exact_byte_match_passes(self):
        assert is_upload_verified(1_048_576, 1_048_576, 'MP4')

    def test_short_upload_fails(self):
        """A truncated upload must never be recorded as backed up."""
        assert not is_upload_verified(1_048_576, 1_048_000, 'MP4')

    def test_missing_in_drive_fails(self):
        assert not is_upload_verified(1_048_576, None, 'MP4')

    def test_audio_is_also_strict(self):
        assert not is_upload_verified(50_000_000, 49_999_999, 'M4A')

    def test_metadata_passes_on_existence(self):
        """Zoom's reported size for metadata files is unreliable."""
        assert is_upload_verified(0, 4_096, 'TIMELINE')
        assert not is_upload_verified(0, None, 'TIMELINE')
