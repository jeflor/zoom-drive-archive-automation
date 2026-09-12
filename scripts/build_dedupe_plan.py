"""Build a safe dedupe plan for the Drive archive: keep one copy per MD5.

Read-only on Drive. Writes dedupe_plan.xlsx for human review and
dedupe_delete_ids.json for drive_trash.py to execute.

Refuses to write a plan that would orphan any unique file. The planning logic
lives in zoomarchive.dedupe, which is unit-tested.

    python3 scripts/build_dedupe_plan.py [data_dir]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoomarchive.dedupe import build_plan

DATA = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('WORK_DIR', 'data')

COLUMNS = ['action', 'size_mb', 'name', 'folder_path', 'kept_copy_folder',
           'file_id']
WIDTHS = {'A': 24, 'B': 10, 'C': 52, 'D': 60, 'E': 46, 'F': 30}


def main():
    with open(os.path.join(DATA, 'drive_index_md5.json'), encoding='utf-8') as fh:
        drive = json.load(fh)
    if isinstance(drive, dict):
        drive = drive['files']

    plan = build_plan(drive)

    # Hard stop: the plan must never be the only thing standing between a
    # unique file and oblivion.
    if plan['orphaned']:
        sys.exit(f"ABORT: {len(plan['orphaned'])} unique file(s) would lose "
                 f"every copy. No plan written.")

    with open(os.path.join(DATA, 'dedupe_delete_ids.json'), 'w',
              encoding='utf-8') as fh:
        json.dump(plan['delete_ids'], fh)

    same_gb = sum(r['size_mb'] for r in plan['rows']
                  if 'same-folder' in r['action']) / 1e3
    cross_gb = sum(r['size_mb'] for r in plan['rows']
                   if 'cross-folder' in r['action']) / 1e3
    total_gb = same_gb + cross_gb

    try:
        import openpyxl
    except ImportError:
        print('openpyxl not installed — skipping the .xlsx review sheet.')
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Delete plan'
        ws.append(COLUMNS)
        for r in plan['rows']:
            ws.append([r[c] for c in COLUMNS])
        for col, w in WIDTHS.items():
            ws.column_dimensions[col].width = w
        ws.freeze_panes = 'A2'

        sm = wb.create_sheet('Summary')
        for row in [
            ['Metric', 'Value'],
            ['Files to trash (redundant copies)', len(plan['delete_ids'])],
            ['  same-folder duplicates (GB)', round(same_gb, 1)],
            ['  cross-folder duplicates (GB)', round(cross_gb, 1)],
            ['TOTAL reclaimed (GB)', round(total_gb, 1)],
            ['Folders fully emptied by this', len(plan['empty_after'])],
            ['INTEGRITY: unique files losing all copies', len(plan['orphaned'])],
            ['Safety', 'one copy of every unique md5 always kept'],
            ['Deletion mode', 'Google Drive trash (recoverable ~30 days)'],
        ]:
            sm.append(row)
        sm.column_dimensions['A'].width = 44
        sm.column_dimensions['B'].width = 52
        wb.save(os.path.join(DATA, 'dedupe_plan.xlsx'))

    print(f"Files to trash: {len(plan['delete_ids'])}  ({total_gb:.1f} GB)")
    print(f'  same-folder: {same_gb:.1f} GB | cross-folder: {cross_gb:.1f} GB')
    print(f"Folders fully emptied: {len(plan['empty_after'])}")
    print(f"INTEGRITY unique files losing ALL copies: {len(plan['orphaned'])}"
          f"  (must be 0)")


if __name__ == '__main__':
    main()
