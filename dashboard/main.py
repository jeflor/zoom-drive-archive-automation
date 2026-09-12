#!/usr/bin/env python3
"""
Zoom backup dashboard — a small Cloud Run service.

Scans the Drive zBackup tree and renders:
  - Overview: LIVE reconciliation of Zoom cloud recordings vs Drive ("Unbacked in
    Zoom") + last backup activity + archive totals
  - Browser:  every host account -> meeting folder, with links into Drive
  - Activity: meetings by backup date
  - Costs:    Cloud Run cost estimate

The at-a-glance question this answers is "is everything backed up RIGHT NOW?" —
by diffing what Zoom still holds (recent, since media is auto-trashed after backup)
against Drive by exact byte size. No frozen worklist; it self-heals.

The Drive scan is expensive, so results are cached. Cloud Scheduler hits /refresh
to keep the cache warm; page loads serve the cached snapshot instantly.

Access is gated by a token in the URL (?t=... or /d/<token>).

Env: DRIVE_ROOT_FOLDER_ID, DRIVE_TOKEN_JSON, DASH_TOKEN, PORT,
     ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, RECONCILE_DAYS
"""
import os, re, json, time, html, base64, threading
from datetime import datetime, timezone, timedelta
from wsgiref.simple_server import make_server

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import google.auth

# --- Cloud Run pricing (us-central1, tier-1, USD) for the cost ESTIMATE ---
CPU_RATE = 0.00002400   # per vCPU-second
MEM_RATE = 0.00000250   # per GiB-second
FREE_CPU = 180000       # free vCPU-seconds / month
FREE_MEM = 360000       # free GiB-seconds / month

_mon = {'creds': None, 'project': None}


def mon_creds():
    if _mon['creds'] is None:
        _mon['creds'], _mon['project'] = google.auth.default(
            scopes=['https://www.googleapis.com/auth/monitoring.read'])
    if not _mon['creds'].valid:
        _mon['creds'].refresh(Request())
    return _mon['creds'].token, _mon['project']


def _metric_sum(metric, start_iso, end_iso, token, project):
    r = _session.get(
        f'https://monitoring.googleapis.com/v3/projects/{project}/timeSeries',
        headers={'Authorization': f'Bearer {token}'},
        params={'filter': f'metric.type="{metric}"',
                'interval.startTime': start_iso, 'interval.endTime': end_iso,
                'aggregation.alignmentPeriod': '86400s',
                'aggregation.perSeriesAligner': 'ALIGN_SUM',
                'aggregation.crossSeriesReducer': 'REDUCE_SUM'}, timeout=60)
    r.raise_for_status()
    total = 0.0
    for ts in r.json().get('timeSeries', []):
        for p in ts.get('points', []):
            v = p['value']
            total += float(v.get('doubleValue') or v.get('int64Value') or 0)
    return total


def cost_estimate():
    """Estimate month-to-date Cloud Run cost from usage metrics. Returns None
    if metrics aren't available (falls back to 'see billing console')."""
    try:
        token, project = mon_creds()
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        s, e = start.strftime('%Y-%m-%dT%H:%M:%SZ'), now.strftime('%Y-%m-%dT%H:%M:%SZ')
        cpu = _metric_sum('run.googleapis.com/container/cpu/allocation_time', s, e, token, project)
        mem = _metric_sum('run.googleapis.com/container/memory/allocation_time', s, e, token, project)
        cpu_cost = max(0, cpu - FREE_CPU) * CPU_RATE
        mem_cost = max(0, mem - FREE_MEM) * MEM_RATE
        return {'cpu_s': cpu, 'mem_gibs': mem, 'cpu_cost': cpu_cost,
                'mem_cost': mem_cost, 'total': cpu_cost + mem_cost,
                'month': start.strftime('%B %Y')}
    except Exception as ex:
        return {'error': str(ex)[:120]}

_session = requests.Session()
_session.mount('https://', HTTPAdapter(
    pool_maxsize=20,
    max_retries=Retry(total=6, connect=6, read=6, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=['GET'])))

ROOT = os.environ['DRIVE_ROOT_FOLDER_ID']
TOKEN_PATH = os.environ.get('DRIVE_TOKEN_JSON', '/secrets/token.json')
DASH_TOKEN = os.environ['DASH_TOKEN']
PORT = int(os.environ.get('PORT', '8080'))
FOLDER_MIME = 'application/vnd.google-apps.folder'

# Zoom read credentials for the live reconciliation (optional — if unset, the
# "Unbacked in Zoom" card shows "n/a" instead of a live figure).
ZOOM_ACCOUNT_ID = os.environ.get('ZOOM_ACCOUNT_ID')
ZOOM_CLIENT_ID = os.environ.get('ZOOM_CLIENT_ID')
ZOOM_CLIENT_SECRET = os.environ.get('ZOOM_CLIENT_SECRET')
RECONCILE_DAYS = int(os.environ.get('RECONCILE_DAYS', '45'))
MEDIA = {'MP4', 'M4A'}
# A layout counts as backed up if Drive holds the same recording_type within this
# size ratio — absorbs Zoom's post-backup re-renders (e.g. the CC view) that shift
# the byte size for identical content, instead of flagging them as false gaps.
RERENDER_TOL = (0.80, 1.20)

_creds = Credentials.from_authorized_user_file(TOKEN_PATH, ['https://www.googleapis.com/auth/drive'])
_lock = threading.Lock()
CACHE = {'built': 0, 'building': False, 'data': None}


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


def zoom_token():
    b = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
    r = _session.post('https://zoom.us/oauth/token',
                      params={'grant_type': 'account_credentials', 'account_id': ZOOM_ACCOUNT_ID},
                      headers={'Authorization': f'Basic {b}'}, timeout=60)
    r.raise_for_status()
    return r.json()['access_token']


def zoom_recent(zt, host, frm, to):
    out = []
    page = None
    while True:
        p = {'from': frm, 'to': to, 'page_size': 300}
        if page:
            p['next_page_token'] = page
        r = _session.get(f'https://api.zoom.us/v2/users/{host}/recordings',
                         headers={'Authorization': f'Bearer {zt}'}, params=p, timeout=60)
        if r.status_code != 200:
            break
        j = r.json()
        out += j.get('meetings', [])
        page = j.get('next_page_token')
        if not page:
            break
    return out


def scan_tree():
    """Fast pass: ROOT -> host folders -> meeting folders (folders-only)."""
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
    return {'hosts': hosts, 'total_files': total_meetings, 'recon': None,
            'last_activity': '', 'ts': time.time(), 'cost': None, 'progress_pending': True}


def reconcile(hosts):
    """LIVE Zoom->Drive diff. For each host, list recent Zoom recordings and check
    every MEDIA file's exact byte size is present in the meeting's Drive folder.
    Returns a summary of what Zoom still holds that Drive doesn't. Because media is
    auto-trashed after backup, Zoom retains only recent recordings, so this set is
    small and fast. Returns None if Zoom creds aren't configured."""
    if not (ZOOM_ACCOUNT_ID and ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET):
        return None
    try:
        zt = zoom_token()
    except Exception as ex:
        return {'error': str(ex)[:120]}
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECONCILE_DAYS)).strftime('%Y-%m-%d')
    host_id = {h['name']: h['id'] for h in hosts}
    unbacked = []
    checked = 0
    for hname, hid in host_id.items():
        try:
            recs = zoom_recent(zt, hname, cutoff, today)
        except Exception:
            continue
        for m in recs:
            media = [f for f in m.get('recording_files', [])
                     if f.get('file_type') in MEDIA and f.get('status') == 'completed']
            if not media:
                continue
            checked += 1
            ts = (m.get('start_time') or '').replace('T', ' ').replace('Z', '')
            fname = re.sub(r'[\\/:*?"<>|]', '_', f"{ts}: {m.get('topic', '')}")[:120]
            esc = fname.replace('\\', '\\\\').replace("'", "\\'")
            try:
                folders = dlist({'q': f"name='{esc}' and '{hid}' in parents and "
                                      f"mimeType='{FOLDER_MIME}' and trashed=false",
                                 'fields': 'files(id)'})
            except Exception:
                folders = []
            dfiles = []  # (name, size)
            for f in folders:
                try:
                    for x in dlist({'q': f"'{f['id']}' in parents and trashed=false",
                                    'fields': 'files(name,size)'}):
                        if x.get('size'):
                            dfiles.append((x.get('name', ''), int(x['size'])))
                except Exception:
                    pass
            sizes = {s for _, s in dfiles}

            def backed(zf):
                """Backed up if Drive has the exact bytes, OR the SAME layout
                (recording_type) at a comparable size — Zoom re-renders some
                layouts (esp. the closed-caption view) after the original backup,
                giving a new byte size for identical content. Tolerance mirrors the
                July-27 Michigan precedent."""
                sz = zf.get('file_size') or 0
                if sz in sizes:
                    return True
                suffix = f"({zf.get('recording_type') or ''})."
                for n, s in dfiles:
                    if suffix in n and sz and RERENDER_TOL[0] <= s / sz <= RERENDER_TOL[1]:
                        return True
                return False

            missing = [f for f in media if not backed(f)]
            if missing:
                unbacked.append({'host': hname, 'start': m.get('start_time', ''),
                                 'topic': m.get('topic', ''), 'files': len(missing),
                                 'bytes': sum(f.get('file_size', 0) for f in missing)})
    unbacked.sort(key=lambda u: u['start'], reverse=True)
    return {'meetings': len(unbacked), 'files': sum(u['files'] for u in unbacked),
            'bytes': sum(u['bytes'] for u in unbacked), 'items': unbacked[:50],
            'checked': checked, 'window_days': RECONCILE_DAYS}


def compute_progress(data):
    """Slow pass: refine Activity dates by most-recent upload, capture last backup
    activity, run the live reconciliation, and estimate cost. Mutates `data`."""
    hosts = data['hosts']
    last_activity = ''
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).strftime('%Y-%m-%dT%H:%M:%SZ')
        recent = dlist({'q': f"trashed=false and mimeType!='{FOLDER_MIME}' and createdTime > '{cutoff}'",
                        'fields': 'files(createdTime,parents)'})
        latest = {}
        for f in recent:
            c = f.get('createdTime', '')
            if c > last_activity:
                last_activity = c
            for pid in f.get('parents', []):
                if c > latest.get(pid, ''):
                    latest[pid] = c
        for h in hosts:
            for m in h['meetings']:
                lc = latest.get(m['id'])
                if lc and lc > m.get('created', ''):
                    m['created'] = lc
    except Exception:
        pass
    data['last_activity'] = last_activity
    data['recon'] = reconcile(hosts)
    data['cost'] = cost_estimate()
    data['progress_pending'] = False


def build_cache(force=False):
    with _lock:
        if CACHE['building']:
            return
        if not force and CACHE['data'] and time.time() - CACHE['built'] < 3600:
            return
        CACHE['building'] = True
    try:
        data = scan_tree()          # fast: page becomes renderable immediately
        CACHE['data'] = data
        CACHE['built'] = time.time()
        compute_progress(data)      # slow: reconciliation + cost fill in after
    finally:
        CACHE['building'] = False


def gb(n):
    return f"{n/1e9:.1f} GB" if n >= 1e9 else f"{n/1e6:.0f} MB"


def ago(iso):
    """Human 'x ago' from an ISO8601 UTC timestamp."""
    if not iso:
        return '—'
    try:
        t = datetime.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except Exception:
        return iso[:10]
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 3600:
        return f"{int(secs//60)}m ago"
    if secs < 86400:
        return f"{int(secs//3600)}h ago"
    return f"{int(secs//86400)}d ago"


def page():
    d = CACHE['data']
    if not d:
        return ("<head><meta http-equiv='refresh' content='5'></head>"
                "<body style='font:16px sans-serif;background:#0f1117;color:#e6e8ee;padding:40px'>"
                "<h2>Building the dashboard…</h2><p>First scan in progress — this page refreshes "
                "itself automatically (about 20–30 seconds on a cold start).</p></body>")
    when = datetime.fromtimestamp(d['ts'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    pending = d.get('progress_pending')
    meetings = sum(len(h['meetings']) for h in d['hosts'])

    # --- Unbacked-in-Zoom card (live reconciliation) ---
    recon = d.get('recon')
    banner = ''
    if pending:
        recon_n, recon_cls, recon_sub = 'checking…', '', ''
    elif recon is None:
        recon_n, recon_cls, recon_sub = 'n/a', '', 'Zoom check not configured'
    elif 'error' in recon:
        recon_n, recon_cls, recon_sub = '?', 'warn', 'check failed — ' + html.escape(recon['error'])
    else:
        n = recon['meetings']
        recon_n = str(n)
        recon_cls = 'ok' if n == 0 else 'warn'
        if n == 0:
            recon_sub = f"all Zoom recordings (last {recon['window_days']}d) are in Drive"
        else:
            recon_sub = f"{recon['files']} file(s) · {gb(recon['bytes'])} · last {recon['window_days']}d"
            items = ''.join(
                f"<tr><td>{u['start'][:10]}</td><td>{html.escape(u['host'])}</td>"
                f"<td>{html.escape(u['topic'][:60])}</td><td>{u['files']}</td>"
                f"<td>{gb(u['bytes'])}</td></tr>" for u in recon['items'])
            banner = ("<div class=warnbox><b>⚠ Not yet in Drive:</b> these recordings are still in "
                      "Zoom cloud with no byte-matched copy in Drive (the next sweep should pick them "
                      "up within ~3h).<table class=mini><thead><tr><th>Date</th><th>Host</th>"
                      "<th>Meeting</th><th>Files</th><th>Size</th></tr></thead><tbody>"
                      f"{items}</tbody></table></div>")

    last_act = 'checking…' if pending else ago(d.get('last_activity', ''))

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

    # costs tab
    c = d.get('cost') or {}
    if 'error' in c or not c:
        costs_html = (f"<p class=sub>Live estimate unavailable{' ('+html.escape(c.get('error',''))+')' if c.get('error') else ''}. "
                      f"See the authoritative figure in the billing console below.</p>")
    else:
        costs_html = f"""<div class=cards>
<div class=card><div class=n>${c['total']:.2f}</div><div class=l>Est. Cloud Run cost · {c['month']}</div></div>
<div class=card><div class=n>${c['cpu_cost']:.2f}</div><div class=l>vCPU ({c['cpu_s']/3600:.0f} vCPU-hrs)</div></div>
<div class=card><div class=n>${c['mem_cost']:.2f}</div><div class=l>Memory ({c['mem_gibs']/3600:.0f} GiB-hrs)</div></div>
</div>
<p class=sub>Estimate from Cloud Run usage metrics × published rates, after the monthly free tier
(180k vCPU-sec, 360k GiB-sec). Egress to Drive is free. This is an approximation — not the invoice.</p>"""
    costs_html += ('<p><a href="https://console.cloud.google.com/billing" target=_blank>'
                   '↗ Open the GCP billing console for actual charges</a></p>')

    # Activity tab
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

    return TEMPLATE.format(
        when=when, recon_n=recon_n, recon_cls=recon_cls, recon_sub=recon_sub, banner=banner,
        last_act=last_act, meetings=meetings, hosts=len(d['hosts']),
        rows="\n".join(rows), costs=costs_html, activity=activity_html)


TEMPLATE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Zoom Backup Dashboard</title><style>
:root{{--bg:#0f1117;--card:#1a1d27;--fg:#e6e8ee;--mut:#8b90a0;--acc:#5b8def;--ok:#3fb950;--warn:#e0a45b}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px;margin-bottom:20px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}}
.card{{background:var(--card);border-radius:10px;padding:16px}}
.card .n{{font-size:24px;font-weight:600}}.card .l{{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.card.ok .n{{color:var(--ok)}}.card.warn .n{{color:var(--warn)}}
.warnbox{{background:#241d12;border:1px solid #4a3a1c;border-radius:10px;padding:14px 16px;margin-bottom:20px;font-size:13px}}
.warnbox b{{color:var(--warn)}}
table.mini{{margin-top:10px}}table.mini td,table.mini th{{padding:4px 8px}}
input{{width:100%;padding:10px 12px;background:var(--card);border:1px solid #2a2e3a;border-radius:8px;color:var(--fg);margin-bottom:12px;font-size:14px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #22252f;font-size:13px}}
th{{color:var(--mut);font-weight:500;position:sticky;top:0;background:var(--bg)}}
td:nth-child(2),td:nth-child(3),td:nth-child(4){{text-align:right;color:var(--mut);white-space:nowrap}}
.warnbox td:nth-child(2),.warnbox td:nth-child(3){{text-align:left}}
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
<h1>Zoom → Google Drive Backup</h1>
<div class=sub>Snapshot {when} · auto-refreshes hourly (9am–9pm ET)</div>
<div class=tabs>
<div class="tab on" onclick="tab(this,'v-arch')">Archive</div>
<div class=tab onclick="tab(this,'v-act')">Activity</div>
<div class=tab onclick="tab(this,'v-cost')">Costs</div>
</div>
<div id=v-arch class="view on">
<div class=cards>
<div class="card {recon_cls}"><div class=n>{recon_n}</div><div class=l>Unbacked in Zoom</div>
<div class=l style="text-transform:none;letter-spacing:0;margin-top:4px">{recon_sub}</div></div>
<div class=card><div class=n>{last_act}</div><div class=l>Last backup activity</div></div>
<div class=card><div class=n>{meetings}</div><div class=l>Meetings archived (all time)</div></div>
<div class=card><div class=n>{hosts}</div><div class=l>Host accounts</div></div>
</div>
{banner}
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
if(document.body.innerText.indexOf('checking…')>-1)setTimeout(function(){{location.reload()}},8000);
if(document.querySelector('.fb'))af(document.querySelector('.fb'),1);
</script></body></html>"""


def app(environ, start):
    path = environ.get('PATH_INFO', '/')
    qs = dict(p.split('=', 1) for p in environ.get('QUERY_STRING', '').split('&') if '=' in p)
    if path == '/healthz':
        start('200 OK', [('Content-Type', 'text/plain')])
        return [b'ok']
    if path == '/refresh':
        if qs.get('t') != DASH_TOKEN:
            start('403 Forbidden', [('Content-Type', 'text/plain')]); return [b'forbidden']
        threading.Thread(target=build_cache, kwargs={'force': True}, daemon=True).start()
        start('200 OK', [('Content-Type', 'text/plain')]); return [b'refresh started']
    tok = qs.get('t') or path.strip('/').split('/')[-1]
    if tok != DASH_TOKEN:
        start('403 Forbidden', [('Content-Type', 'text/plain')]); return [b'Add ?t=<token> to the URL']
    if not CACHE['data'] and not CACHE['building']:
        threading.Thread(target=build_cache, kwargs={'force': True}, daemon=True).start()
    start('200 OK', [('Content-Type', 'text/html; charset=utf-8')])
    return [page().encode('utf-8')]


# warm the cache on startup, in the background so the server binds $PORT fast
threading.Thread(target=build_cache, kwargs={'force': True}, daemon=True).start()

if __name__ == '__main__':
    make_server('', PORT, app).serve_forever()
