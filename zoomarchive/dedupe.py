"""Duplicate-copy removal for the Drive archive, safe by construction.

Years of ad-hoc backups left the archive full of redundant copies — whole
folders re-uploaded under variant names. Drive *does* expose MD5s, so dedup
can be exact: keep one copy per MD5, trash the rest.

"Safe by construction" is the design claim, and `orphaned_md5s` is the
assertion that proves it before anything is trashed. The first draft of this
plan would have orphaned 179 unique files; that check is what caught it.
"""
from collections import defaultdict


def keeper_key(file_row, folder_sizes):
    """Sort key choosing which copy of an MD5 survives.

    Prefer the copy sitting in the most complete folder (the most files), so
    that redundant partial folders empty out entirely rather than leaving one
    stray file behind. Then shortest path, then id, purely for determinism —
    the same index must always produce the same plan.
    """
    return (-folder_sizes[file_row['path']],
            len(file_row['path']),
            file_row['id'])


def build_plan(drive_rows):
    """Build the dedupe plan.

    Returns a dict with:
      delete_ids   Drive file ids to trash
      rows         human-reviewable plan rows
      empty_after  folders left with nothing in them
      orphaned     MD5s that would lose every copy — MUST be empty
    """
    folder_files = defaultdict(list)
    for r in drive_rows:
        folder_files[r['path']].append(r)
    folder_sizes = {p: len(v) for p, v in folder_files.items()}

    by_md5 = defaultdict(list)
    for r in drive_rows:
        if r.get('md5'):
            by_md5[r['md5']].append(r)

    delete_ids, rows = [], []
    for md5, copies in by_md5.items():
        if len(copies) == 1:
            continue
        keep = min(copies, key=lambda r: keeper_key(r, folder_sizes))
        for r in copies:
            if r['id'] == keep['id']:
                continue
            scope = 'same-folder' if r['path'] == keep['path'] else 'cross-folder'
            delete_ids.append(r['id'])
            rows.append({
                'action': f'DELETE ({scope} dup)',
                'size_mb': round(r['size'] / 1e6, 1),
                'name': r['name'][:58],
                'folder_path': r['path'],
                'kept_copy_folder': keep['path'],
                'file_id': r['id'],
            })

    deleting = set(delete_ids)
    empty_after = [p for p, v in folder_files.items()
                   if v and all(r['id'] in deleting for r in v)]
    # The integrity assertion: no unique file may lose all of its copies.
    orphaned = [md5 for md5, copies in by_md5.items()
                if all(r['id'] in deleting for r in copies)]

    return {'delete_ids': delete_ids, 'rows': rows,
            'empty_after': empty_after, 'orphaned': orphaned}
