#!/usr/bin/env python3
"""
LOCAL, self-contained snapshot of the Zoom backup dashboard.

Runs the same Drive scan + progress computation as the deployed Cloud Run
service (dashboard/main.py), but instead of serving over HTTP it writes a
single static HTML file you can open in a browser. Use this while GCP billing
is off and the Cloud Run dashboard is suspended.

The Archive and Activity tabs are fully functional (they read the Drive API
directly with token.json). The Costs tab needs Cloud Monitoring, which requires
billing, so it shows an "unavailable locally" note instead.

Usage:
    ./venv/bin/python dashboard/local_snapshot.py
Then open the printed path in your browser. Re-run any time for a fresh scan.

Env overrides (all optional): DRIVE_ROOT_FOLDER_ID, DRIVE_TOKEN_JSON,
WORKLIST, OUT.
"""
import os, re, json, time, html, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
TOKEN_PATH = os.environ.get('DRIVE_TOKEN_JSON',
                            os.path.join(os.path.dirname(_HERE), 'token.json'))
WORKLIST = os.environ.get('WORKLIST', os.path.join(_HERE, 'backup_worklist.json'))
OUT = os.environ.get('OUT', os.path.join(_HERE, 'dashboard_snapshot.html'))
FOLDER_MIME = 'application/vnd.google-apps.folder'

_session = requests.Session()
_session.mount('https://', HTTPAdapter(
    pool_maxsize=20,
    max_retries=Retry(total=6, connect=6, read=6, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=['GET'])))

_creds = Credentials.from_authorized_user_file(TOKEN_PATH, ['https://www.googleapis.com/auth/drive'])
_lock = threading.Lock()


def dtoken():
    with _lock:
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token


def dlist(params):
    p = {'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
         'corpora': 'allDrives', 'pageSize': '1000', **params}
    out = []
    page = None
    while True:
        if page:
            p['pageToken'] = page
        r = _session.get('https://www.googleapis.com/drive/v3/files',
                         headers={'Authorization': f'Bearer {dtoken()}'}, params=p, timeout=60)
        r.raise_for_status()
        d = r.json()
        out += d.get('files', [])
        page = d.get('nextPageToken')
        if not page:
            return out


def scan_tree():
    host_folders = sorted(
        dlist({'q': f"'{ROOT}' in parents and trashed=false and mimeType='{FOLDER_MIME}'",
               'fields': 'files(id,name)'}),
        key=lambda x: x['name'])
    hosts = []
    total_meetings = 0
    for hf in host_folders:
        try:
            mfs = dlist({'q': f"'{hf['id']}' in parents and trashed=false and mimeType='{FOLDER_MIME}'",
                         'fields': 'files(id,name,createdTime)'})
        except Exception:
            mfs = []
        meetings = sorted([{'name': m['name'], 'id': m['id'], 'created': m.get('createdTime', '')} for m in mfs],
                          key=lambda m: m['name'], reverse=True)
        hosts.append({'name': hf['name'], 'id': hf['id'], 'meetings': meetings})
        total_meetings += len(meetings)
        print(f"  {hf['name']}: {len(meetings)} meetings")
    return {'hosts': hosts, 'total_files': total_meetings,
            'work_done': 0, 'work_total': 0, 'gb_present': 0, 'gb_expected': 0,
            'ts': time.time(), 'progress_pending': True}


def compute_progress(data):
    hosts = data['hosts']
    try:
        work = json.load(open(WORKLIST))
        host_id = {h['name']: h['id'] for h in hosts}

        def media_bytes(fid):
            try:
                files = dlist({'q': f"'{fid}' in parents and trashed=false",
                               'fields': 'files(size,mimeType)'})
            except Exception:
                return None
            return sum(int(f.get('size', 0) or 0) for f in files
                       if f['mimeType'] != FOLDER_MIME and int(f.get('size', 0) or 0) >= 1_000_000)

        def present(w):
            ts = (w.get('start_time') or '').replace('T', ' ').replace('Z', '')
            fname = re.sub(r'[\\/:*?"<>|]', '_', f"{ts}: {w['topic']}")[:120]
            hn = re.sub(r'[\\/:*?"<>|]', '_', w['host'])[:120]
            exp = sum(f['size'] for f in w['missing_media'])
            hid = host_id.get(hn)
            if not hid or exp <= 0:
                return (0, exp)
            esc = fname.replace('\\', '\\\\').replace("'", "\\'")
            try:
                folders = dlist({'q': f"name='{esc}' and '{hid}' in parents and "
                                      f"mimeType='{FOLDER_MIME}' and trashed=false",
                                 'fields': 'files(id)'})
            except Exception:
                return (0, exp)
            best = 0
            for f in folders:
                mb = media_bytes(f['id'])
                if mb is not None:
                    best = max(best, mb)
            return (best, exp)

        print(f"computing backfill progress over {len(work)} worklist meetings…")
        with ThreadPoolExecutor(max_workers=12) as ex:
            pres = list(ex.map(present, work))
        data['work_total'] = len(work)
        data['gb_expected'] = sum(e for _, e in pres)
        data['gb_present'] = sum(min(p, e) if e else 0 for p, e in pres)
        data['work_done'] = sum(1 for p, e in pres if e and p >= 0.99 * e)
    except Exception as ex:
        print("progress computation skipped:", ex)

    # Refine backup dates for the Activity log by most-recent upload.
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).strftime('%Y-%m-%dT%H:%M:%SZ')
        recent = dlist({'q': f"trashed=false and mimeType!='{FOLDER_MIME}' and createdTime > '{cutoff}'",
                        'fields': 'files(createdTime,parents)'})
        latest = {}
        for f in recent:
            c = f.get('createdTime', '')
            for pid in f.get('parents', []):
                if c > latest.get(pid, ''):
                    latest[pid] = c
        for h in hosts:
            for m in h['meetings']:
                lc = latest.get(m['id'])
                if lc and lc > m.get('created', ''):
                    m['created'] = lc
    except Exception as ex:
        print("activity-date refinement skipped:", ex)
    data['progress_pending'] = False


def render(d):
    when = datetime.fromtimestamp(d['ts'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    gp, ge = d.get('gb_present', 0), d.get('gb_expected', 0)
    pct = round(100 * gp / ge) if ge else 0
    gbp_s = f"{gp/1e9:.0f} / {ge/1e9:.0f} GB"
    pct_s = f'{pct}% by data'
    meetings = sum(len(h['meetings']) for h in d['hosts'])
    rows = []
    for i, h in enumerate(d['hosts']):
        hlink = f"https://drive.google.com/drive/folders/{h['id']}"
        rows.append(
            f"<tr class='host' data-h='{i}' data-host='{html.escape(h['name'].lower())}' onclick='tog({i})'>"
            f"<td><span class='car' id='c{i}'>▶</span> "
            f"<a href='{hlink}' target='_blank' onclick='event.stopPropagation()'>📁 {html.escape(h['name'])}</a></td>"
            f"<td>{len(h['meetings'])} meetings</td></tr>")
        for m in h['meetings']:
            mlink = f"https://drive.google.com/drive/folders/{m['id']}"
            rows.append(
                f"<tr class='mtg' data-h='{i}' data-host='{html.escape(h['name'].lower())}' "
                f"data-name='{html.escape(m['name'].lower())}' style='display:none'>"
                f"<td class='ind'><a href='{mlink}' target='_blank'>{html.escape(m['name'])}</a></td>"
                f"<td></td></tr>")

    costs_html = ("<p class=sub>The Costs tab estimates Cloud Run spend from Cloud Monitoring, "
                  "which needs GCP billing enabled. This is a local snapshot generated on your "
                  "computer while billing is off, so there's nothing to bill and no live metrics "
                  "to read.</p>"
                  '<p><a href="https://console.cloud.google.com/billing" target=_blank>'
                  '↗ Open the GCP billing console</a></p>')

    acts = []
    for h in d['hosts']:
        for m in h['meetings']:
            if m.get('created'):
                acts.append((m['created'], h['name'], m['name'], m['id']))
    acts.sort(reverse=True)
    arows = []
    for created, host, mname, mid in acts:
        day = created[:10]
        link = f"https://drive.google.com/drive/folders/{mid}"
        arows.append(
            f"<tr class='act' data-day='{day}'>"
            f"<td class='ad'>{day}</td>"
            f"<td>{html.escape(host)}</td>"
            f"<td><a href='{link}' target='_blank'>{html.escape(mname)}</a></td></tr>")
    activity_html = (
        "<div class='filt'>Show backed up in: "
        "<button class='fb on' onclick=\"af(this,1)\">Today</button>"
        "<button class='fb' onclick=\"af(this,7)\">7 days</button>"
        "<button class='fb' onclick=\"af(this,30)\">30 days</button>"
        "<button class='fb' onclick=\"af(this,3650)\">All</button>"
        "<span id='acount' class='sub'></span></div>"
        "<table><thead><tr><th>Backed up</th><th>Host</th><th>Meeting (→ Drive folder)</th></tr></thead>"
        f"<tbody id='atb'>{''.join(arows)}</tbody></table>")
    done_s = f"{d['work_done']}/{d['work_total']}"
    return TEMPLATE.format(
        when=when, pct=pct, gbp_s=gbp_s, pct_s=pct_s, done_s=done_s,
        meetings=meetings, hosts=len(d['hosts']), rows="\n".join(rows), costs=costs_html,
        activity=activity_html)


TEMPLATE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Zoom Backup Dashboard (local snapshot)</title><style>
:root{{--bg:#0f1117;--card:#1a1d27;--fg:#e6e8ee;--mut:#8b90a0;--acc:#5b8def;--ok:#3fb950}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px;margin-bottom:20px}}
.badge{{display:inline-block;background:#3a2a12;color:#e0a45b;border:1px solid #5a4020;border-radius:6px;padding:2px 8px;font-size:12px;margin-left:8px;vertical-align:middle}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}}
.card{{background:var(--card);border-radius:10px;padding:16px}}
.card .n{{font-size:24px;font-weight:600}}.card .l{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.bar{{height:10px;background:#2a2e3a;border-radius:6px;overflow:hidden;margin:8px 0 4px}}
.bar>i{{display:block;height:100%;background:var(--ok);width:{pct}%}}
input{{width:100%;padding:10px 12px;background:var(--card);border:1px solid #2a2e3a;border-radius:8px;color:var(--fg);margin-bottom:12px;font-size:14px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #22252f;font-size:13px}}
th{{color:var(--mut);font-weight:500;position:sticky;top:0;background:var(--bg)}}
td:nth-child(2),td:nth-child(3),td:nth-child(4){{text-align:right;color:var(--mut);white-space:nowrap}}
tr.host{{cursor:pointer}}tr.host:hover td{{background:#1e2230}}
tr.host td{{background:#161922;font-weight:600;border-top:1px solid #2a2e3a}}
tr.host td:first-child a{{color:var(--fg)}}
.car{{display:inline-block;width:14px;color:var(--mut);font-size:11px}}
.ind{{padding-left:40px!important}}a{{color:var(--acc);text-decoration:none}}a:hover{{text-decoration:underline}}
.tabs{{display:flex;gap:6px;margin-bottom:18px;border-bottom:1px solid #2a2e3a}}
.tab{{padding:8px 16px;cursor:pointer;color:var(--mut);border-bottom:2px solid transparent;font-size:14px}}
.tab.on{{color:var(--fg);border-bottom-color:var(--acc)}}
.view{{display:none}}.view.on{{display:block}}
.filt{{margin-bottom:14px;color:var(--mut);font-size:13px}}
.fb{{margin:0 4px;padding:5px 12px;background:var(--card);border:1px solid #2a2e3a;border-radius:6px;color:var(--mut);cursor:pointer;font-size:13px}}
.fb.on{{color:var(--fg);border-color:var(--acc)}}
td.ad,td.act{{white-space:nowrap;color:var(--mut)}}
</style></head><body><div class=wrap>
<h1>Zoom → Google Drive Backup <span class=badge>local snapshot</span></h1>
<div class=sub>Snapshot {when} · generated locally (Cloud Run dashboard is offline while billing is disabled) · re-run the script to refresh</div>
<div class=tabs>
<div class="tab on" onclick="tab(this,'v-arch')">Archive</div>
<div class=tab onclick="tab(this,'v-act')">Activity</div>
<div class=tab onclick="tab(this,'v-cost')">Costs</div>
</div>
<div id=v-arch class="view on">
<div class=cards>
<div class=card><div class=n>{gbp_s}</div><div class=l>Backfill transferred</div>
<div class=bar><i></i></div><div class=l>{pct_s}</div></div>
<div class=card><div class=n>{done_s}</div><div class=l>Meetings fully backed up</div></div>
<div class=card><div class=n>{meetings}</div><div class=l>Meetings archived (all time)</div></div>
<div class=card><div class=n>{hosts}</div><div class=l>Host accounts</div></div>
</div>
<input id=q placeholder="Filter by host or meeting…" oninput="f()">
<table><thead><tr><th>Meeting / Host folder</th><th></th></tr></thead>
<tbody id=tb>{rows}</tbody></table>
</div>
<div id=v-act class=view>{activity}</div>
<div id=v-cost class=view>{costs}</div>
</div><script>
var AF_DAYS=1;
function af(el,days){{AF_DAYS=days;
document.querySelectorAll('.fb').forEach(function(b){{b.classList.remove('on')}});el.classList.add('on');
var cut=new Date(Date.now()-days*86400000).toISOString().slice(0,10),n=0;
document.querySelectorAll('tr.act').forEach(function(r){{
var show=r.dataset.day>=cut;r.style.display=show?'':'none';if(show)n++;}});
document.getElementById('acount').textContent='  '+n+' recording'+(n==1?'':'s');}}
function tab(el,id){{
document.querySelectorAll('.tab').forEach(function(t){{t.classList.remove('on')}});
document.querySelectorAll('.view').forEach(function(v){{v.classList.remove('on')}});
el.classList.add('on');document.getElementById(id).classList.add('on');}}
function tog(i){{var open=false;
document.querySelectorAll("tr.mtg[data-h='"+i+"']").forEach(function(r){{
r.style.display=(r.style.display==='none')?'':'none';open=(r.style.display!=='none');}});
document.getElementById('c'+i).textContent=open?'▼':'▶';}}
function f(){{var q=document.getElementById('q').value.toLowerCase().trim();
document.querySelectorAll('tr.host').forEach(function(h){{var i=h.dataset.h,any=false;
var hm=q&&(h.dataset.host||'').includes(q);
document.querySelectorAll("tr.mtg[data-h='"+i+"']").forEach(function(r){{
var show=q&&(hm||(r.dataset.name||'').includes(q));
r.style.display=show?'':'none';if(show)any=true;}});
document.getElementById('c'+i).textContent=(q&&(any||hm))?'▼':'▶';
h.style.display=(q&&!any&&!hm)?'none':'';}});}}
if(document.querySelector('.fb'))af(document.querySelector('.fb'),1);
</script></body></html>"""


def main():
    print(f"scanning Drive root {ROOT} …")
    data = scan_tree()
    compute_progress(data)
    with open(OUT, 'w') as fh:
        fh.write(render(data))
    print(f"\nwrote {OUT}")


if __name__ == '__main__':
    main()
