"""Full lay of the land after Phase-1 deletion + manual deletes.
Reads the fresh zoom_recordings.json (current live Zoom) and drive_index.json.
Produces state_of_land.xlsx with clean tabs and prints a summary."""
import sys, json
from collections import defaultdict, Counter
import openpyxl

zoom = json.load(open('zoom_recordings.json'))
drive = json.load(open('drive_index.json'))
MEDIA={'MP4','M4A'}
gsize=Counter(); folder_sizes=defaultdict(Counter); size_paths=defaultdict(list)
for f in drive['files']:
    gsize[f['size']]+=1; folder_sizes[f['path']][f['size']]+=1; size_paths[f['size']].append(f['path'])
def best_folder(media):
    sizes=sorted((f['size'] for f in media),reverse=True); cand=set(size_paths.get(sizes[0],[]))
    best,bn=None,-1
    for p in cand:
        av=Counter(folder_sizes[p]); n=0
        for s in sizes:
            if av[s]>0: av[s]-=1; n+=1
        if n>bn: best,bn=p,n
    return best,bn

# undeletable set derived from batch media still live after deletion (stuck.json)
undeletable={}  # uuid -> reason
import os
if os.path.exists('stuck.json'):
    for s in json.load(open('stuck.json')):
        undeletable[s['uuid']]='Zoom IQ lock (code 3332)'

cats=defaultdict(list)
for m in zoom:
    media=[f for f in m['files'] if f['file_type'] in MEDIA and f['size']>=1_000_000]
    nonmedia=[f for f in m['files'] if f['file_type'] not in MEDIA or f['size']<1_000_000]
    row_size=sum(f['size'] for f in m['files'])
    rec={'host':m['host'],'date':(m['start_time'] or '')[:10],'topic':m['topic'][:70],
         'total_GB':round(row_size/1e9,2),'media_files':len(media),
         'nonmedia_files':len(nonmedia),'uuid':m['uuid']}
    if not media:
        # only transcripts/small stuff remain (we deleted media, or never had media)
        cats['text_only'].append(rec); continue
    need=Counter(f['size'] for f in media)
    backed = not any(gsize[s]<need[s] for s in need)
    if backed:
        bf,n=best_folder(media)
        rec['drive_folder']=bf
        if m['uuid'] in undeletable:
            rec['reason']=undeletable[m['uuid']]
            cats['backed_up_stuck'].append(rec)   # backed up but couldn't delete
        else:
            cats['backed_up_media_present'].append(rec)  # backed up, media still in Zoom (deletable)
    else:
        matched=sum(1 for f in media if gsize[f['size']]>0)
        rec['media_matched']=matched
        if matched==0: cats['not_backed_up'].append(rec)
        else: cats['partial'].append(rec)

def gb(rows): return round(sum(r['total_GB'] for r in rows),1)
print(f"Live Zoom now: {len(zoom)} meetings")
print(f"{'category':28} {'meetings':>9} {'GB':>8}")
labels=[('backed_up_media_present','Backed up, media still in Zoom'),
        ('backed_up_stuck','Backed up but UNDELETABLE (Zoom IQ)'),
        ('text_only','Text-only left (media already trashed)'),
        ('partial','Partially backed up'),
        ('not_backed_up','NOT backed up (need to back up)')]
for k,lbl in labels:
    print(f"{lbl:36} {len(cats[k]):6} {gb(cats[k]):8}")

wb=openpyxl.Workbook(); first=True
def sheet(title,rows):
    global first
    ws=wb.active if first else wb.create_sheet(); first=False; ws.title=title[:31]
    if not rows: ws.append(['(none)']); return
    cols=[c for c in ['host','date','topic','total_GB','media_files','nonmedia_files','media_matched','reason','drive_folder','uuid'] if c in rows[0]]
    ws.append(cols)
    for r in rows: ws.append([r.get(c,'') for c in cols])
    for i,c in enumerate(cols):
        ws.column_dimensions[chr(65+i)].width={'topic':46,'drive_folder':60,'uuid':30,'reason':26}.get(c,13)
    ws.freeze_panes='A2'
sheet('NOT backed up', sorted(cats['not_backed_up'],key=lambda r:-r['total_GB']))
sheet('Partial', sorted(cats['partial'],key=lambda r:-r['total_GB']))
sheet('Undeletable (Zoom IQ)', cats['backed_up_stuck'])
sheet('Backed up media still in Zoom', sorted(cats['backed_up_media_present'],key=lambda r:-r['total_GB']))
sheet('Text-only remaining', cats['text_only'])
wb.save('state_of_land.xlsx')
print("\nWrote state_of_land.xlsx")
