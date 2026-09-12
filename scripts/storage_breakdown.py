import json
from collections import defaultdict
z = json.load(open('zoom_recordings.json'))
by_ft = defaultdict(lambda:[0,0]); by_rt = defaultdict(lambda:[0,0])
for m in z:
    for f in m['files']:
        by_ft[f['file_type']][0]+=1; by_ft[f['file_type']][1]+=f['size']
        by_rt[f['recording_type']][0]+=1; by_rt[f['recording_type']][1]+=f['size']
print("By file_type:")
for k,(n,b) in sorted(by_ft.items(), key=lambda x:-x[1][1]):
    print(f"  {str(k):14} {n:6} files {b/1e9:9.1f} GB")
print("\nBy recording_type (layout):")
for k,(n,b) in sorted(by_rt.items(), key=lambda x:-x[1][1]):
    print(f"  {str(k):40} {n:6} {b/1e9:9.1f} GB")
tot=sum(b for _,(n,b) in by_ft.items())
print(f"\nTOTAL {tot/1e9:.1f} GB")
# what if we keep only ONE video layout per meeting (largest) + audio + non-video?
VID={'shared_screen_with_speaker_view','shared_screen_with_gallery_view','gallery_view','active_speaker','shared_screen','shared_screen_with_speaker_view(CC)'}
kept=0
for m in z:
    vids=[f for f in m['files'] if (f['recording_type'] or '') in VID]
    other=[f for f in m['files'] if (f['recording_type'] or '') not in VID]
    if vids: kept+=max(f['size'] for f in vids)
    kept+=sum(f['size'] for f in other)
print(f"If Zoom billed only the LARGEST video layout per meeting: {kept/1e9:.1f} GB")
