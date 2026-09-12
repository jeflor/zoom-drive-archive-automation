"""Reconcile Zoom cloud recordings against the Drive archive tree.

Read-only. Classifies every Zoom meeting as SAFE_TO_DELETE / PARTIAL /
NOT_BACKED_UP / NO_MEDIA by matching true byte sizes, and writes
reconciliation.csv + reconciliation.json for the delete tooling to consume.

The classification rules themselves live in zoomarchive.verification, which is
unit-tested — this script is only I/O and reporting.

    python3 scripts/reconcile.py [data_dir]
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoomarchive.verification import CLASSIFICATION_ORDER, reconcile

DATA = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('WORK_DIR', 'data')


def load(name):
    with open(os.path.join(DATA, name), encoding='utf-8') as fh:
        return json.load(fh)


def main():
    zoom = load('zoom_recordings.json')
    drive = load('drive_index_md5.json')
    # Older index files nested the list under a "files" key.
    if isinstance(drive, dict):
        drive = drive['files']

    rows = reconcile(zoom, drive)

    with open(os.path.join(DATA, 'reconciliation.csv'), 'w', newline='',
              encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(DATA, 'reconciliation.json'), 'w',
              encoding='utf-8') as fh:
        json.dump(rows, fh, indent=1)

    counts, gb = Counter(), Counter()
    by_host = defaultdict(Counter)
    for r in rows:
        counts[r['classification']] += 1
        gb[r['classification']] += r['size_gb']
        by_host[r['host']][r['classification']] += 1

    print('=== Reconciliation: Zoom cloud (live) vs Drive archive ===\n')
    print(f"{'class':16} {'meetings':>9} {'GB':>9}")
    for k in CLASSIFICATION_ORDER:
        if counts[k]:
            print(f'{k:16} {counts[k]:9} {gb[k]:9.1f}')
    print(f"{'TOTAL':16} {sum(counts.values()):9} {sum(gb.values()):9.1f}")

    print('\nBy host account:')
    print(f"{'host':30} {'SAFE':>6} {'PARTIAL':>8} {'GAP':>6}")
    for h in sorted(by_host):
        c = by_host[h]
        print(f"{h:30} {c['SAFE_TO_DELETE']:6} {c['PARTIAL']:8} "
              f"{c['NOT_BACKED_UP']:6}")

    print(f"\nWrote reconciliation.csv and reconciliation.json to {DATA}/")
    print(f"SAFE_TO_DELETE would free {gb['SAFE_TO_DELETE']:.0f} GB from Zoom.")
    if counts['PARTIAL']:
        print(f"{counts['PARTIAL']} PARTIAL meeting(s) need backfill before "
              f"any deletion — these are never auto-deleted.")


if __name__ == '__main__':
    main()
