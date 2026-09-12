"""Folder-level verification of the SAFE_TO_DELETE set (media-delete /
transcript-keep policy). Read-only. Writes safe_to_delete.xlsx."""
import sys, json
from collections import defaultdict, Counter
import openpyxl

drive = json.load(open('drive_index.json'))
zoom = json.load(open('zoom_recordings.json'))
MEDIA_TYPES = {'MP4','M4A'}

folder_sizes = defaultdict(Counter)
size_paths = defaultdict(list)
global_sizes = Counter()
for f in drive['files']:
    folder_sizes[f['path']][f['size']] += 1
    size_paths[f['size']].append(f['path'])
    global_sizes[f['size']] += 1

def best_folder(media):
    sizes = sorted((f['size'] for f in media), reverse=True)
    cand = set(size_paths.get(sizes[0], []))
    best, best_n = None, -1
    for p in cand:
        avail = Counter(folder_sizes[p]); n = 0
        for s in sizes:
            if avail[s] > 0: avail[s] -= 1; n += 1
        if n > best_n: best, best_n = p, n
    return best, best_n

def split_path(p):
    # p like "/training@example.com/<meeting folder>"
    parts = [x for x in (p or '').split('/') if x]
    acct = parts[0] if parts else ''
    meeting = '/'.join(parts[1:]) if len(parts) > 1 else ''
    return acct, meeting

confirmed = []
free_bytes = 0
for m in zoom:
    media = [f for f in m['files'] if f['file_type'] in MEDIA_TYPES and f['size'] >= 1_000_000]
    if not media: continue
    need = Counter(f['size'] for f in media)
    if any(global_sizes[s] < need[s] for s in need): continue  # not fully backed up
    bf, n = best_folder(media)
    if n != len(media): continue  # require co-located in one folder
    acct, meeting = split_path(bf)
    free_bytes += sum(f['size'] for f in media)
    confirmed.append({
        'host': m['host'], 'date': (m['start_time'] or '')[:10], 'topic': m['topic'][:80],
        'media_files': len(media), 'media_GB': round(sum(f['size'] for f in media)/1e9, 2),
        'drive_account_folder': acct, 'drive_meeting_folder': meeting,
        'drive_full_path': bf, 'uuid': m['uuid'],
        'delete_file_ids': ';'.join(f['id'] for f in media),
    })

wb = openpyxl.Workbook()
ws = wb.active; ws.title = 'Confirmed Safe (media)'
cols = list(confirmed[0].keys())
ws.append(cols)
for r in confirmed: ws.append([r[c] for c in cols])
# widen the readable columns
widths = {'A':22,'B':12,'C':46,'D':11,'E':10,'F':26,'G':52,'H':60,'I':30,'J':40}
for col,w in widths.items(): ws.column_dimensions[col].width = w
ws.freeze_panes = 'A2'

sm = wb.create_sheet('Summary')
for r in [
    ['Metric','Value'],
    ['Confirmed safe meetings (media in one Drive folder)', len(confirmed)],
    ['Media GB freed if deleted', round(free_bytes/1e9,1)],
    ['Policy','Delete MP4+M4A only; keep transcript / CC / chat / summary'],
    ['Deletion mode','Zoom trash (recoverable ~30 days)'],
]:
    sm.append(r)
sm.column_dimensions['A'].width = 52; sm.column_dimensions['B'].width = 60
wb.save('safe_to_delete.xlsx')
print(f"Confirmed: {len(confirmed)} meetings, {free_bytes/1e9:.1f} GB media. Rewrote xlsx.")
# show a few full paths to prove they're intact
for r in confirmed[:6]:
    print(f"  {r['drive_full_path']}")
