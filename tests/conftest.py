import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'webhook'))


def media(size, **kw):
    """A media recording file of an exact byte size."""
    row = {'id': kw.pop('id', f'f{size}'), 'file_type': 'MP4',
           'recording_type': 'active_speaker', 'size': size}
    row.update(kw)
    return row


def meeting(*files, **kw):
    return {'host': kw.get('host', 'archive@example.com'),
            'uuid': kw.get('uuid', 'AAAA=='),
            'meeting_id': kw.get('meeting_id', 81234567890),
            'topic': kw.get('topic', 'Weekly Team Call'),
            'start_time': kw.get('start_time', '2026-05-01T18:00:00Z'),
            'total_size': sum(f['size'] for f in files),
            'files': list(files)}


def drive(size, path='/archive@example.com/2026-05-01 Call', **kw):
    row = {'id': kw.pop('id', f'd{size}'), 'name': kw.pop('name', f'{size}.mp4'),
           'path': path, 'size': size, 'md5': kw.pop('md5', f'md5-{size}')}
    row.update(kw)
    return row
