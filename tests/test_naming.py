"""Archive naming. Meeting topics are user-typed and often hostile to paths."""
from zoomarchive.naming import MAX_NAME, recording_filename, sanitize


def test_path_separators_are_replaced():
    """A slash in a topic would otherwise fork the archive tree."""
    assert sanitize('Q&A: intake/review') == 'Q&A_ intake_review'


def test_every_illegal_character_is_handled():
    assert sanitize(r'a\b/c:d*e?f"g<h>i|j') == 'a_b_c_d_e_f_g_h_i_j'


def test_long_names_are_capped():
    assert len(sanitize('x' * 500)) == MAX_NAME


def test_unicode_and_spacing_are_preserved():
    assert sanitize('Café — session #2') == 'Café — session #2'


def test_recording_filename_shape():
    assert recording_filename('2026-05-01 18_00_00', 'Weekly Call',
                              'gallery_view', 'MP4') == \
        '2026-05-01 18_00_00 Weekly Call (gallery_view).mp4'


def test_recording_filename_sanitises_the_topic():
    name = recording_filename('2026-05-01', 'A/B: test', 'active_speaker', '.mp4')
    assert '/' not in name
    assert name.endswith('.mp4')


def test_distinct_layouts_never_collide():
    """Each rendered layout must land as its own file in the same folder."""
    names = {recording_filename('2026-05-01', 'Call', layout, 'MP4')
             for layout in ('gallery_view', 'active_speaker', 'shared_screen')}
    assert len(names) == 3
