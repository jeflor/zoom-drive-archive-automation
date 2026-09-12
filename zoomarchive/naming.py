"""Archive folder/file naming.

Drive tolerates more than most filesystems, but the archive is also browsed
through synced desktop clients, so names are reduced to a portable set. The
length cap keeps deep paths under Drive's per-path limits.
"""
import re

ILLEGAL = re.compile(r'[\\/:*?"<>|]')
MAX_NAME = 120


def sanitize(name: str) -> str:
    """Replace filesystem-illegal characters and cap the length."""
    return ILLEGAL.sub('_', name)[:MAX_NAME]


def recording_filename(start_time: str, topic: str, recording_type: str,
                       extension: str) -> str:
    """Canonical name for one archived recording file.

    Meeting topics are user-typed and routinely contain slashes and colons,
    which is why every component goes through `sanitize`.
    """
    ext = extension.lower().lstrip('.')
    return sanitize(f'{start_time} {topic} ({recording_type})') + f'.{ext}'
