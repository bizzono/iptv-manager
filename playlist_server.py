#!/usr/bin/env python3
"""
playlist_server.py — Dynamic IPTV M3U server with browser management UI.
Runs on http://<mini-ip>:8765

Endpoints:
  GET  /playlist.m3u         — Live M3U (point Jellyfin here)
  GET  /                     — Channel management UI
  GET  /api/channels         — All channels as JSON
  POST /api/channels         — Add a channel (JSON body)
  PUT  /api/channels/<idx>   — Update channel by row index
  DELETE /api/channels/<idx> — Delete channel by row index
"""

import csv
import os
import re
from flask import Flask, request, jsonify, Response
import xml.etree.ElementTree as ET
import time as _time

app = Flask(__name__)

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.csv")

FIELDNAMES = ["name", "group", "resolution", "fps", "bitrate", "tvg_id", "tvg_logo", "url", "source"]


# ── CSV helpers ───────────────────────────────────────────────────────────────

def read_channels():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r.get("name", "").strip()]


def write_channels(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({f: r.get(f, "") for f in FIELDNAMES})


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text.strip("-")


# ── M3U generation ────────────────────────────────────────────────────────────

def generate_m3u(rows):
    host = os.environ.get("SERVER_HOST", "localhost")
    lines = ['#EXTM3U x-tvg-url="http://{}:8765/epg.xml"'.format(host)]
    for r in rows:
        name       = r.get("name", "").strip()
        group      = r.get("group", "Sports").strip()
        resolution = r.get("resolution", "").strip()
        fps        = r.get("fps", "").strip()
        bitrate    = r.get("bitrate", "").strip()
        tvg_id     = r.get("tvg_id", "").strip()
        tvg_logo   = r.get("tvg_logo", "").strip()
        url        = r.get("url", "").strip()
        if not url:
            url = "http://YOUR_STREAM_URL_HERE/{}.m3u8".format(slugify(name))

        # Append tvg_id as a unique query param so channels sharing the same
        # stream URL get distinct Jellyfin channel IDs (M3U parser hashes the URL).
        if tvg_id:
            sep = "&" if "?" in url else "?"
            url = "{}{}{}".format(url, sep, "_id={}".format(tvg_id))

        attrs = []
        if tvg_id:     attrs.append('tvg-id="{}"'.format(tvg_id))
        if tvg_logo:   attrs.append('tvg-logo="{}"'.format(tvg_logo))
        if group:      attrs.append('group-title="{}"'.format(group))
        if resolution: attrs.append('tvg-resolution="{}"'.format(resolution))
        if fps:        attrs.append('tvg-fps="{}"'.format(fps))
        if bitrate:    attrs.append('tvg-bitrate="{}"'.format(bitrate))

        attr_str = (" " + " ".join(attrs)) if attrs else ""
        lines.append("#EXTINF:-1{},{}".format(attr_str, name))
        lines.append(url)
    return "\n".join(lines) + "\n"


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/playlist.m3u")
def playlist():
    rows = read_channels()
    source_filter = request.args.get("source")
    if source_filter:
        rows = [r for r in rows if r.get("source", "").strip() == source_filter]
    # include all channels — sportsgames use localhost:8766 proxy (MPEG-TS, working)
    content = generate_m3u(rows)
    return Response(
        content,
        mimetype="application/x-mpegurl",
        headers={"Content-Disposition": "inline; filename=playlist.m3u"},
    )


@app.route("/epg.xml")
def epg_xml():
    """Generate XMLTV EPG from channels.csv — IDs match M3U tvg-id perfectly."""
    rows = read_channels()
    now = int(_time.time())
    root = ET.Element("tv", attrib={"generator-info-name": "playlist-server"})

    # Build channel elements
    for r in rows:
        tid = (r.get("tvg_id") or "").strip()
        if not tid:
            continue
        ch = ET.SubElement(root, "channel", id=tid)
        ET.SubElement(ch, "display-name").text = r.get("name", "")
        logo = (r.get("tvg_logo") or "").strip()
        if logo:
            ET.SubElement(ch, "icon", src=logo)

    # Build programme elements for sportsgames/tvsportslive channels
    # Use a wide window: started 30m ago, ends 8h from now
    start_ts = now - 1800
    stop_ts  = now + 28800

    def ts(t):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    sport_category_map = {
        "Sports - MLB": "Baseball", "Sports - NBA": "Basketball",
        "Sports - NFL": "American Football", "Sports - NHL": "Ice Hockey",
        "Sports - MMA": "Martial Arts", "Sports - Boxing": "Boxing",
        "Sports - WNBA": "Basketball", "Sports - CFL": "American Football",
        "Sports - Soccer": "Soccer", "Sports - F1": "Motor Racing",
        "Sports - MotoGP": "Motor Racing", "Sports - WWE": "Wrestling",
        "Sports - NCAA": "College Sports", "Sports - Tennis": "Tennis",
        "Sports - Golf": "Golf", "Sports - Rugby": "Rugby",
        "Sports - Cricket": "Cricket", "Sports - Live": "Sports",
        "Sports": "Sports",
    }

    for r in rows:
        tid = (r.get("tvg_id") or "").strip()
        source = (r.get("source") or "").strip()
        if not tid or source not in ("sportsgames", "tvsportslive"):
            continue
        group = r.get("group", "Sports")
        category = sport_category_map.get(group, "Sports")
        prog = ET.SubElement(root, "programme",
                             start=ts(start_ts), stop=ts(stop_ts), channel=tid)
        ET.SubElement(prog, "title").text = r.get("name", "")
        ET.SubElement(prog, "category").text = category
        ET.SubElement(prog, "desc").text = "Live sports event"
        plogo = (r.get("tvg_logo") or "").strip()
        if plogo:
            ET.SubElement(prog, "icon", src=plogo)

    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    return Response(xml_str, mimetype="application/xml")



@app.route("/api/channels", methods=["GET"])
def api_list():
    return jsonify(read_channels())


@app.route("/api/channels", methods=["POST"])
def api_add():
    data = request.get_json(force=True)
    rows = read_channels()
    row = {f: data.get(f, "") for f in FIELDNAMES}
    rows.append(row)
    write_channels(rows)
    return jsonify({"ok": True, "index": len(rows) - 1})


@app.route("/api/channels/<int:idx>", methods=["PUT"])
def api_update(idx):
    data = request.get_json(force=True)
    rows = read_channels()
    if idx < 0 or idx >= len(rows):
        return jsonify({"error": "index out of range"}), 404
    for f in FIELDNAMES:
        if f in data:
            rows[idx][f] = data[f]
    write_channels(rows)
    return jsonify({"ok": True})


@app.route("/api/channels/<int:idx>", methods=["DELETE"])
def api_delete(idx):
    rows = read_channels()
    if idx < 0 or idx >= len(rows):
        return jsonify({"error": "index out of range"}), 404
    rows.pop(idx)
    write_channels(rows)
    return jsonify({"ok": True})


# ── Management UI ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IPTV Playlist Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f0f13;color:#e0e0e0;padding:24px}
h1{font-size:1.4rem;margin-bottom:20px;color:#fff}
.toolbar{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
button{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:600}
.btn-add{background:#3b82f6;color:#fff}.btn-add:hover{background:#2563eb}
.btn-save{background:#22c55e;color:#fff}.btn-save:hover{background:#16a34a}
.btn-del{background:#ef4444;color:#fff;padding:4px 10px;font-size:.75rem}.btn-del:hover{background:#dc2626}
.m3u-url{font-size:.82rem;margin-left:auto;display:flex;align-items:center;gap:8px;color:#94a3b8}
.m3u-url a{color:#60a5fa;font-family:monospace;text-decoration:none}.m3u-url a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#1e1e2e;padding:8px 10px;text-align:left;color:#94a3b8;font-weight:600;position:sticky;top:0;z-index:1}
td{padding:6px 8px;border-bottom:1px solid #1e1e2e;vertical-align:middle}
tr:hover td{background:#1a1a28}
input,select{background:#1e1e2e;border:1px solid #333;color:#e0e0e0;padding:4px 6px;border-radius:4px;width:100%;font-size:.8rem}
input:focus,select:focus{outline:1px solid #3b82f6;border-color:#3b82f6}
.url-input{font-family:monospace;font-size:.73rem}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:700}
.b4k{background:#7c3aed;color:#fff}.b1080{background:#2563eb;color:#fff}
.b720{background:#0891b2;color:#fff}.bother{background:#374151;color:#ccc}
.notice{background:#1e3a1e;border:1px solid #22c55e;border-radius:6px;padding:9px 14px;
        margin-bottom:16px;font-size:.82rem;color:#86efac;display:none}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:100;
       align-items:center;justify-content:center}
.modal.open{display:flex}
.mbox{background:#1e1e2e;border-radius:10px;padding:24px;width:480px;max-width:95vw}
.mbox h2{margin-bottom:16px;font-size:1rem;color:#fff}
.frow{margin-bottom:12px}
.frow label{display:block;font-size:.78rem;color:#94a3b8;margin-bottom:4px}
.mbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
.btn-cancel{background:#374151;color:#e0e0e0}.btn-cancel:hover{background:#4b5563}
.cnt{color:#666;font-size:.78rem;margin-left:4px}
</style>
</head>
<body>
<h1>IPTV Playlist Manager</h1>
<div class="notice" id="notice"></div>
<div class="toolbar">
  <button class="btn-add" onclick="openAdd()">+ Add Channel</button>
  <button class="btn-save" onclick="copyUrl()">Copy Jellyfin URL</button>
  <span class="cnt" id="cnt"></span>
  <span class="m3u-url">Live M3U: <a id="m3u-link" href="/playlist.m3u" target="_blank">/playlist.m3u</a></span>
</div>
<table>
<thead><tr>
  <th style="width:36px">#</th>
  <th>Name</th><th>Group</th><th>Res</th><th>FPS</th><th>Bitrate</th>
  <th>Stream URL (edit inline or click Edit)</th><th style="width:100px"></th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>

<div class="modal" id="modal">
  <div class="mbox">
    <h2 id="mtitle">Add Channel</h2>
    <div class="frow"><label>Name *</label><input id="f-name" placeholder="ESPN+"></div>
    <div class="frow"><label>Group</label><input id="f-group" value="Sports"></div>
    <div class="frow"><label>Resolution</label>
      <select id="f-resolution">
        <option value="">—</option>
        <option value="4k">4K</option>
        <option value="1080p">1080p</option>
        <option value="720p">720p</option>
        <option value="480p">480p</option>
        <option value="360p">360p</option>
      </select>
    </div>
    <div class="frow"><label>FPS</label>
      <select id="f-fps">
        <option value="">—</option>
        <option value="60">60</option>
        <option value="30">30</option>
        <option value="25">25</option>
      </select>
    </div>
    <div class="frow"><label>Bitrate (kbps)</label><input id="f-bitrate" placeholder="4500" type="number"></div>
    <div class="frow"><label>TVG ID (EPG)</label><input id="f-tvg_id" placeholder="espn.us"></div>
    <div class="frow"><label>Logo URL</label><input id="f-tvg_logo" placeholder="https://...logo.png"></div>
    <div class="frow"><label>Stream URL</label><input id="f-url" placeholder="https://host/stream.m3u8"></div>
    <div class="mbtns">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-save" onclick="saveChannel()">Save</button>
    </div>
  </div>
</div>

<script>
let channels = [], editIdx = null;

async function load() {
  const r = await fetch('/api/channels');
  channels = await r.json();
  render();
}

function badgeClass(res) {
  if (!res) return 'bother';
  const r = res.toLowerCase();
  if (r === '4k') return 'b4k';
  if (r.includes('1080')) return 'b1080';
  if (r.includes('720')) return 'b720';
  return 'bother';
}

function esc(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function render() {
  document.getElementById('cnt').textContent = channels.length + ' channels';
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = channels.map((c, i) => {
    const hasUrl = c.url && !c.url.includes('YOUR_STREAM_URL');
    const urlColor = hasUrl ? '#22c55e' : '#f87171';
    return '<tr>' +
      '<td style="color:#555">' + (i+1) + '</td>' +
      '<td><strong>' + esc(c.name) + '</strong></td>' +
      '<td>' + esc(c.group) + '</td>' +
      '<td><span class="badge ' + badgeClass(c.resolution) + '">' + (esc(c.resolution)||'—') + '</span></td>' +
      '<td>' + (esc(c.fps)||'—') + '</td>' +
      '<td>' + (c.bitrate ? c.bitrate+'k' : '—') + '</td>' +
      '<td><input class="url-input" value="' + esc(c.url) + '" placeholder="https://…" ' +
        'style="color:' + urlColor + '" ' +
        'onchange="quickUrl(' + i + ',this.value)"></td>' +
      '<td style="white-space:nowrap">' +
        '<button class="btn-add" style="padding:4px 10px;font-size:.75rem;margin-right:4px" onclick="openEdit(' + i + ')">Edit</button>' +
        '<button class="btn-del" onclick="del(' + i + ')">✕</button>' +
      '</td>' +
    '</tr>';
  }).join('');
}

async function quickUrl(idx, url) {
  await fetch('/api/channels/' + idx, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: url})
  });
  channels[idx].url = url;
  notify('Saved URL for ' + channels[idx].name);
  render();
}

function notify(msg) {
  const el = document.getElementById('notice');
  el.textContent = msg; el.style.display = 'block';
  clearTimeout(notify._t);
  notify._t = setTimeout(() => el.style.display = 'none', 3000);
}

function openAdd() {
  editIdx = null;
  document.getElementById('mtitle').textContent = 'Add Channel';
  ['name','tvg_id','tvg_logo','url','bitrate'].forEach(f => { document.getElementById('f-'+f).value = ''; });
  document.getElementById('f-group').value = 'Sports';
  document.getElementById('f-resolution').value = '1080p';
  document.getElementById('f-fps').value = '60';
  document.getElementById('modal').classList.add('open');
}

function openEdit(idx) {
  editIdx = idx;
  const c = channels[idx];
  document.getElementById('mtitle').textContent = 'Edit Channel';
  ['name','group','resolution','fps','bitrate','tvg_id','tvg_logo','url'].forEach(f => {
    document.getElementById('f-'+f).value = c[f] || '';
  });
  document.getElementById('modal').classList.add('open');
}

function closeModal() { document.getElementById('modal').classList.remove('open'); }

async function saveChannel() {
  const data = {};
  ['name','group','resolution','fps','bitrate','tvg_id','tvg_logo','url'].forEach(f => {
    data[f] = (document.getElementById('f-'+f).value || '').trim();
  });
  if (!data.name) { alert('Name is required'); return; }
  if (editIdx !== null) {
    await fetch('/api/channels/' + editIdx, {
      method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
    });
    notify('Updated: ' + data.name);
  } else {
    await fetch('/api/channels', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
    });
    notify('Added: ' + data.name);
  }
  closeModal(); load();
}

async function del(idx) {
  if (!confirm('Delete "' + channels[idx].name + '"?')) return;
  await fetch('/api/channels/' + idx, {method: 'DELETE'});
  notify('Deleted');
  load();
}

function copyUrl() {
  const url = window.location.protocol + '//' + window.location.hostname + ':8765/playlist.m3u';
  navigator.clipboard.writeText(url).then(() => notify('Copied: ' + url));
}

document.getElementById('m3u-link').href = window.location.protocol + '//' + window.location.hostname + ':8765/playlist.m3u';
load();
</script>
</body>
</html>
"""


@app.route("/")
def ui():
    return Response(HTML, mimetype="text/html")




# ESPN CDN base
_ESPN = "https://a.espncdn.com/combiner/i?img=/i/teamlogos"

SPORT_LOGO_URLS = {
    'nfl':    f"{_ESPN}/leagues/500/nfl.png",
    'nba':    f"{_ESPN}/leagues/500/nba.png",
    'mlb':    f"{_ESPN}/leagues/500/mlb.png",
    'nhl':    f"{_ESPN}/leagues/500/nhl.png",
    'wnba':   f"{_ESPN}/leagues/500/wnba.png",
    'mma':    f"{_ESPN}/leagues/500/ufc.png",
    'soccer': f"{_ESPN}/leagues/500/mls.png",
    'ncaa':   f"{_ESPN}/leagues/500/ncaa.png",
    'f1':     None,
    'motogp': None,
    'boxing': None,
    'wwe':    None,
    'cfl':    None,
    'tennis': None,
    'golf':   None,
    'rugby':  None,
    'cricket':None,
    'live':   None,
}

# MLB team name → ESPN abbreviation
MLB_TEAMS = {
    'yankees': 'nyy', 'red sox': 'bos', 'mariners': 'sea', 'twins': 'min',
    'diamondbacks': 'ari', 'orioles': 'bal', 'dodgers': 'lad', 'angels': 'laa',
    'athletics': 'oak', 'astros': 'hou', 'pirates': 'pit', 'rockies': 'col',
    'guardians': 'cle', 'mets': 'nym', 'phillies': 'phi', 'nationals': 'was',
    'rays': 'tb', 'giants': 'sf', 'marlins': 'mia', 'brewers': 'mil',
    'braves': 'atl', 'cubs': 'chc', 'cardinals': 'stl', 'padres': 'sd',
    'rangers': 'tex', 'blue jays': 'tor', 'tigers': 'det',
    'white sox': 'chw', 'reds': 'cin', 'royals': 'kc',
}

# NBA team name → ESPN abbreviation
NBA_TEAMS = {
    'lakers': 'lal', 'celtics': 'bos', 'warriors': 'gs', 'bulls': 'chi',
    'heat': 'mia', 'nets': 'bkn', 'knicks': 'ny', 'sixers': 'phi',
    'suns': 'phx', 'nuggets': 'den', 'bucks': 'mil', 'hawks': 'atl',
    'jazz': 'utah', 'clippers': 'lac', 'mavericks': 'dal', 'rockets': 'hou',
    'spurs': 'sa', 'raptors': 'tor', 'thunder': 'okc', 'timberwolves': 'min',
    'pacers': 'ind', 'cavaliers': 'cle', 'pistons': 'det', 'magic': 'orl',
    'wizards': 'wsh', 'pelicans': 'no', 'trail blazers': 'por', 'kings': 'sac',
    'grizzlies': 'mem', 'hornets': 'cha',
}

# NFL team name → ESPN abbreviation  
NFL_TEAMS = {
    'patriots': 'ne', 'cowboys': 'dal', 'packers': 'gb', 'chiefs': 'kc',
    '49ers': 'sf', 'ravens': 'bal', 'bills': 'buf', 'bengals': 'cin',
    'eagles': 'phi', 'rams': 'lar', 'buccaneers': 'tb', 'steelers': 'pit',
    'broncos': 'den', 'chargers': 'lac', 'bears': 'chi', 'colts': 'ind',
    'vikings': 'min', 'saints': 'no', 'seahawks': 'sea', 'falcons': 'atl',
    'giants': 'nyg', 'jets': 'nyj', 'lions': 'det', 'browns': 'cle',
    'commanders': 'wsh', 'texans': 'hou', 'jaguars': 'jax', 'titans': 'ten',
    'raiders': 'lv', 'cardinals': 'ari', 'dolphins': 'mia', 'panthers': 'car',
}

SPORT_SVG_FALLBACKS = {
    'f1':     ('#E10600', '#ffffff', 'F1'),
    'motogp': ('#004B8D', '#E10600', 'MGP'),
    'boxing': ('#E65100', '#ffffff', 'BOX'),
    'wwe':    ('#D32F2F', '#ffffff', 'WWE'),
    'cfl':    ('#CC0000', '#ffffff', 'CFL'),
    'tennis': ('#558B2F', '#ffffff', 'TEN'),
    'golf':   ('#1B5E20', '#ffffff', 'GOLF'),
    'rugby':  ('#880E4F', '#ffffff', 'RUG'),
    'cricket':('#4A148C', '#ffffff', 'CRI'),
    'live':   ('#1976D2', '#ffffff', 'LIVE'),
}


def make_sport_svg(bg, fg, label):
    font_size = 28 if len(label) <= 3 else 22
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" height="120">
  <circle cx="60" cy="60" r="58" fill="{bg}"/>
  <text x="60" y="60" dy=".35em" text-anchor="middle" fill="{fg}"
        font-family="Arial,Helvetica,sans-serif" font-weight="bold" font-size="{font_size}">{label}</text>
</svg>'''


@app.route("/icon/<sport>")
def sport_icon(sport):
    key = sport.lower().replace('.svg', '').replace('.png', '')
    cdn_url = SPORT_LOGO_URLS.get(key)
    if cdn_url:
        return Response('', status=302, headers={'Location': cdn_url, 'Cache-Control': 'public, max-age=86400'})
    bg, fg, label = SPORT_SVG_FALLBACKS.get(key, SPORT_SVG_FALLBACKS['live'])
    svg = make_sport_svg(bg, fg, label)
    return Response(svg, mimetype='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})


@app.route("/teamlogo/<league>/<team_name>")
def team_logo(league, team_name):
    """Return ESPN team logo by team name (e.g. /teamlogo/mlb/twins)"""
    name = team_name.lower().replace('-', ' ').replace('_', ' ')
    league = league.lower()
    team_maps = {'mlb': (MLB_TEAMS, 'mlb'), 'nba': (NBA_TEAMS, 'nba'), 'nfl': (NFL_TEAMS, 'nfl')}
    if league in team_maps:
        teams, espn_league = team_maps[league]
        abbrev = teams.get(name)
        if abbrev:
            url = f"{_ESPN}/{espn_league}/500/{abbrev}.png"
            return Response('', status=302, headers={'Location': url, 'Cache-Control': 'public, max-age=86400'})
    # Fallback to league logo
    return sport_icon(league)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765, debug=False)
