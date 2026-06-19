#!/usr/bin/env python3
"""
IPTV Manager — Dynamic M3U server with TV Guide, EPG, and source import.
Single-file deployment. Port 8765.

Pages:  /           TV Guide (now/next + schedule panel)
        /channels   Channel editor
        /sources    M3U source URLs — add any website's M3U and import channels
        /settings   EPG sources + status

API:    /playlist.m3u       Live M3U output (Jellyfin tuner URL)
        /epg.xml            XMLTV output (Jellyfin guide URL, embedded in M3U header)
"""

import csv, gzip, io, json, os, re, sqlite3, threading, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "channels.csv")
DB_PATH  = os.path.join(BASE_DIR, "iptv.db")

FIELDNAMES      = ["name", "group", "resolution", "fps", "bitrate", "tvg_id", "tvg_logo", "url", "source"]
EPG_REFRESH_H   = 12
EPG_KEEP_DAYS   = 3

# ─── Database ────────────────────────────────────────────────────────────────

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS programmes (
            channel_id TEXT NOT NULL,
            start      INTEGER NOT NULL,
            stop       INTEGER NOT NULL,
            title      TEXT,
            subtitle   TEXT,
            desc       TEXT,
            category   TEXT,
            PRIMARY KEY (channel_id, start)
        );
        CREATE INDEX IF NOT EXISTS idx_prog_ch    ON programmes(channel_id);
        CREATE INDEX IF NOT EXISTS idx_prog_start ON programmes(start);

        CREATE TABLE IF NOT EXISTS epg_sources (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            url          TEXT NOT NULL UNIQUE,
            enabled      INTEGER DEFAULT 1,
            last_fetched INTEGER DEFAULT 0,
            last_count   INTEGER DEFAULT 0,
            last_error   TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS m3u_sources (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            url          TEXT NOT NULL UNIQUE,
            enabled      INTEGER DEFAULT 1,
            auto_sync    INTEGER DEFAULT 0,
            last_synced  INTEGER DEFAULT 0,
            last_count   INTEGER DEFAULT 0,
            last_error   TEXT DEFAULT ''
        );
        """)

# ─── CSV helpers ─────────────────────────────────────────────────────────────

def read_channels():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("name", "").strip()]

def write_channels(rows):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({fn: r.get(fn, "") for fn in FIELDNAMES})

def slugify(t):
    t = re.sub(r"[^a-z0-9\s-]", "", t.lower())
    return re.sub(r"[\s-]+", "-", t).strip("-")

# ─── M3U parser ──────────────────────────────────────────────────────────────

_ATTR_RE = re.compile(r'([\w-]+)=["\']?([^"\'>\s]+)["\']?')

def parse_m3u(text):
    """Parse M3U text into a list of channel dicts. Handles any well-formed M3U."""
    channels = []
    lines = text.splitlines()
    meta = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTM3U"):
            i += 1
            continue
        if line.startswith("#EXTINF"):
            meta = {}
            # parse key=value attributes
            for k, v in _ATTR_RE.findall(line):
                meta[k.lower().replace("-", "_")] = v
            # channel display name is after the last comma
            if "," in line:
                meta["name"] = line.split(",", 1)[1].strip()
            i += 1
            # skip non-URL comment lines
            while i < len(lines) and lines[i].strip().startswith("#"):
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    res, fps = _guess_res_fps(meta, url)
                    channels.append({
                        "name":       meta.get("name", "Unknown"),
                        "group":      meta.get("group_title", "Uncategorised"),
                        "resolution": meta.get("tvg_resolution", res),
                        "fps":        meta.get("tvg_fps", fps),
                        "bitrate":    meta.get("tvg_bitrate", ""),
                        "tvg_id":     meta.get("tvg_id", ""),
                        "tvg_logo":   meta.get("tvg_logo", ""),
                        "url":        url,
                        "source":     "",
                    })
            i += 1
        else:
            i += 1
    return channels

def _guess_res_fps(meta, url):
    """Infer resolution/fps hints from name and URL when not in attributes."""
    name = (meta.get("name", "") + " " + url).lower()
    res = ""
    fps = ""
    if "4k" in name or "uhd" in name or "2160" in name:
        res = "4k"
    elif "1080" in name or "fhd" in name:
        res = "1080p"
    elif "720" in name or "hd" in name:
        res = "720p"
    elif "480" in name or "sd" in name:
        res = "480p"
    elif "360" in name:
        res = "360p"
    if "60fps" in name or "60p" in name:
        fps = "60"
    elif "30fps" in name or "30p" in name:
        fps = "30"
    return res, fps

def fetch_url(url, timeout=30):
    """Fetch a URL, handle gzip, return decoded text."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "IPTV-Manager/2.0", "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")

# ─── M3U generation ──────────────────────────────────────────────────────────

def generate_m3u(rows):
    host = os.environ.get("SERVER_HOST", "localhost")
    out = ['#EXTM3U x-tvg-url="http://{}:8765/epg.xml"'.format(host)]
    for r in rows:
        name   = r.get("name", "").strip()
        tvg_id = r.get("tvg_id", "").strip() or slugify(name)
        url    = r.get("url", "").strip() or "http://YOUR_STREAM_URL_HERE/{}.m3u8".format(slugify(name))
        attrs  = ['tvg-id="{}"'.format(tvg_id)]
        if r.get("tvg_logo"): attrs.append('tvg-logo="{}"'.format(r["tvg_logo"]))
        attrs.append('group-title="{}"'.format(r.get("group", "General")))
        if r.get("resolution"): attrs.append('tvg-resolution="{}"'.format(r["resolution"]))
        if r.get("fps"):        attrs.append('tvg-fps="{}"'.format(r["fps"]))
        if r.get("bitrate"):    attrs.append('tvg-bitrate="{}"'.format(r["bitrate"]))
        out.append("#EXTINF:-1 {},{}" .format(" ".join(attrs), name))
        out.append(url)
    return "\n".join(out) + "\n"

# ─── XMLTV generation ────────────────────────────────────────────────────────

def generate_xmltv(rows):
    since = int(time.time()) - EPG_KEEP_DAYS * 86400
    root  = ET.Element("tv", attrib={"generator-info-name": "iptv-manager"})
    ids   = set()
    for r in rows:
        tid = (r.get("tvg_id") or slugify(r.get("name", ""))).strip()
        ids.add(tid)
        ch = ET.SubElement(root, "channel", id=tid)
        ET.SubElement(ch, "display-name").text = r.get("name", "")
        if r.get("tvg_logo"):
            ET.SubElement(ch, "icon", src=r["tvg_logo"])

    def ts(t):
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")

    if ids:
        with db() as c:
            progs = c.execute(
                "SELECT * FROM programmes WHERE channel_id IN ({}) AND stop>? ORDER BY channel_id,start".format(
                    ",".join("?" * len(ids))
                ),
                list(ids) + [since],
            ).fetchall()
        for p in progs:
            pg = ET.SubElement(root, "programme", start=ts(p["start"]), stop=ts(p["stop"]), channel=p["channel_id"])
            ET.SubElement(pg, "title").text   = p["title"] or ""
            if p["subtitle"]: ET.SubElement(pg, "sub-title").text = p["subtitle"]
            if p["desc"]:     ET.SubElement(pg, "desc").text      = p["desc"]
            if p["category"]: ET.SubElement(pg, "category").text  = p["category"]

    return ET.tostring(root, encoding="unicode")

# ─── EPG fetch ───────────────────────────────────────────────────────────────

def _parse_xmltv_ts(s):
    s = s.strip()
    dt_str, tz_str = (s.split(" ", 1) + ["+0000"])[:2]
    dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    sign = 1 if tz_str[0] == "+" else -1
    off  = sign * (int(tz_str[1:3]) * 3600 + int(tz_str[3:5]) * 60)
    return int(dt.timestamp()) - off

def fetch_epg(url):
    raw = fetch_url(url)
    root = ET.fromstring(raw)
    progs = []
    for p in root.findall("programme"):
        try:
            progs.append({
                "channel_id": p.get("channel", ""),
                "start":    _parse_xmltv_ts(p.get("start", "0")),
                "stop":     _parse_xmltv_ts(p.get("stop",  "0")),
                "title":    (p.findtext("title")    or "").strip(),
                "subtitle": (p.findtext("sub-title")or "").strip(),
                "desc":     (p.findtext("desc")     or "").strip(),
                "category": (p.findtext("category") or "").strip(),
            })
        except Exception:
            pass
    return progs

def refresh_epg():
    with db() as c:
        sources = c.execute("SELECT * FROM epg_sources WHERE enabled=1").fetchall()
    for s in sources:
        try:
            progs  = fetch_epg(s["url"])
            cutoff = int(time.time()) - EPG_KEEP_DAYS * 86400
            with db() as c:
                c.execute("DELETE FROM programmes WHERE start<?", (cutoff,))
                c.executemany(
                    "INSERT OR REPLACE INTO programmes VALUES(:channel_id,:start,:stop,:title,:subtitle,:desc,:category)",
                    progs,
                )
                c.execute("UPDATE epg_sources SET last_fetched=?,last_count=?,last_error='' WHERE id=?",
                          (int(time.time()), len(progs), s["id"]))
        except Exception as e:
            with db() as c:
                c.execute("UPDATE epg_sources SET last_fetched=?,last_error=? WHERE id=?",
                          (int(time.time()), str(e)[:200], s["id"]))

def _epg_loop():
    while True:
        refresh_epg()
        time.sleep(EPG_REFRESH_H * 3600)

def _source_autosync_loop():
    """Auto-sync M3U sources marked auto_sync=1 every 12 hours."""
    while True:
        time.sleep(EPG_REFRESH_H * 3600)
        with db() as c:
            sources = c.execute("SELECT * FROM m3u_sources WHERE enabled=1 AND auto_sync=1").fetchall()
        for s in sources:
            channels, err = sync_m3u_source(s["id"])
            if err:
                print("[AutoSync] ERROR {}: {}".format(s["name"], err))
            else:
                print("[AutoSync] {} — {} channels".format(s["name"], len(channels)))

# ─── M3U source sync ─────────────────────────────────────────────────────────

def sync_m3u_source(sid):
    with db() as c:
        s = c.execute("SELECT * FROM m3u_sources WHERE id=?", (sid,)).fetchone()
    if not s:
        return None, "Source not found"
    try:
        text     = fetch_url(s["url"])
        channels = parse_m3u(text)
        with db() as c:
            c.execute("UPDATE m3u_sources SET last_synced=?,last_count=?,last_error='' WHERE id=?",
                      (int(time.time()), len(channels), sid))
        return channels, None
    except Exception as e:
        with db() as c:
            c.execute("UPDATE m3u_sources SET last_synced=?,last_error=? WHERE id=?",
                      (int(time.time()), str(e)[:200], sid))
        return None, str(e)

# ─── Guide helpers ────────────────────────────────────────────────────────────

def now_next(ch_id, ts):
    with db() as c:
        now  = c.execute("SELECT * FROM programmes WHERE channel_id=? AND start<=? AND stop>? ORDER BY start DESC LIMIT 1",
                         (ch_id, ts, ts)).fetchone()
        nxt  = c.execute("SELECT * FROM programmes WHERE channel_id=? AND start>? ORDER BY start LIMIT 1",
                         (ch_id, ts)).fetchone()
    return dict(now) if now else None, dict(nxt) if nxt else None

# ─── Channel API ──────────────────────────────────────────────────────────────

@app.route("/playlist.m3u")
def route_m3u():
    return Response(generate_m3u(read_channels()), mimetype="application/x-mpegurl")

@app.route("/epg.xml")
def route_epg():
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + generate_xmltv(read_channels())
    return Response(xml, mimetype="application/xml")

@app.route("/api/channels")
def api_ch_list():
    return jsonify(read_channels())

@app.route("/api/channels", methods=["POST"])
def api_ch_add():
    d = request.get_json(force=True)
    rows = read_channels()
    rows.append({fn: d.get(fn, "") for fn in FIELDNAMES})
    write_channels(rows)
    return jsonify({"ok": True})

@app.route("/api/channels/<int:idx>", methods=["PUT"])
def api_ch_update(idx):
    d = request.get_json(force=True)
    rows = read_channels()
    if not 0 <= idx < len(rows): return jsonify({"error": "not found"}), 404
    rows[idx].update({fn: d[fn] for fn in FIELDNAMES if fn in d})
    write_channels(rows)
    return jsonify({"ok": True})

@app.route("/api/channels/<int:idx>", methods=["DELETE"])
def api_ch_del(idx):
    rows = read_channels()
    if not 0 <= idx < len(rows): return jsonify({"error": "not found"}), 404
    rows.pop(idx)
    write_channels(rows)
    return jsonify({"ok": True})

@app.route("/api/channels/reorder", methods=["POST"])
def api_ch_reorder():
    d = request.get_json(force=True)  # {from_idx, to_idx}
    rows = read_channels()
    fi, ti = d.get("from"), d.get("to")
    if fi is None or ti is None: return jsonify({"error": "bad request"}), 400
    rows.insert(ti, rows.pop(fi))
    write_channels(rows)
    return jsonify({"ok": True})

@app.route("/api/channels/export.csv")
def api_ch_export_csv():
    rows = read_channels()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDNAMES, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({fn: r.get(fn, "") for fn in FIELDNAMES})
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=channels.csv"},
    )

@app.route("/api/channels/bulk_delete", methods=["POST"])
def api_ch_bulk_del():
    idxs = sorted(request.get_json(force=True).get("indices", []), reverse=True)
    rows = read_channels()
    for i in idxs:
        if 0 <= i < len(rows): rows.pop(i)
    write_channels(rows)
    return jsonify({"ok": True, "deleted": len(idxs)})

# ─── M3U source API ───────────────────────────────────────────────────────────

@app.route("/api/sources")
def api_src_list():
    with db() as c:
        return jsonify([dict(r) for r in c.execute("SELECT * FROM m3u_sources ORDER BY id")])

@app.route("/api/sources", methods=["POST"])
def api_src_add():
    d = request.get_json(force=True)
    name = d.get("name", "").strip()
    url  = d.get("url",  "").strip()
    auto = int(d.get("auto_sync", 0))
    if not name or not url: return jsonify({"error": "name and url required"}), 400
    with db() as c:
        c.execute("INSERT OR IGNORE INTO m3u_sources (name,url,auto_sync) VALUES (?,?,?)", (name, url, auto))
    return jsonify({"ok": True})

@app.route("/api/sources/<int:sid>", methods=["PUT"])
def api_src_update(sid):
    d = request.get_json(force=True)
    with db() as c:
        c.execute("UPDATE m3u_sources SET name=?,url=?,auto_sync=? WHERE id=?",
                  (d.get("name"), d.get("url"), d.get("auto_sync", 0), sid))
    return jsonify({"ok": True})

@app.route("/api/sources/<int:sid>", methods=["DELETE"])
def api_src_del(sid):
    with db() as c:
        c.execute("DELETE FROM m3u_sources WHERE id=?", (sid,))
    return jsonify({"ok": True})

@app.route("/api/sources/<int:sid>/toggle", methods=["POST"])
def api_src_toggle(sid):
    with db() as c:
        c.execute("UPDATE m3u_sources SET enabled=1-enabled WHERE id=?", (sid,))
    return jsonify({"ok": True})

@app.route("/api/sources/<int:sid>/sync")
def api_src_sync(sid):
    """Fetch source URL, parse M3U, return channel list for preview. Does NOT import yet."""
    channels, err = sync_m3u_source(sid)
    if err: return jsonify({"error": err}), 500
    return jsonify({"channels": channels, "count": len(channels)})

@app.route("/api/sources/<int:sid>/import", methods=["POST"])
def api_src_import(sid):
    """Import selected channels from a source. Body: {channels: [...], replace_source: bool}"""
    d = request.get_json(force=True)
    incoming    = d.get("channels", [])
    replace_src = d.get("replace_source", False)

    with db() as c:
        src = c.execute("SELECT name FROM m3u_sources WHERE id=?", (sid,)).fetchone()
    src_name = src["name"] if src else str(sid)

    existing = read_channels()
    existing_urls = {r.get("url", "").strip() for r in existing if r.get("url")}

    if replace_src:
        # Remove all channels previously imported from this source
        existing = [r for r in existing if r.get("source", "") != src_name]

    added = 0
    for ch in incoming:
        url = ch.get("url", "").strip()
        if url in existing_urls and not replace_src:
            continue
        ch["source"] = src_name
        existing.append({fn: ch.get(fn, "") for fn in FIELDNAMES})
        existing_urls.add(url)
        added += 1

    write_channels(existing)
    return jsonify({"ok": True, "added": added})

@app.route("/api/sources/parse", methods=["POST"])
def api_src_parse():
    """Parse raw M3U text (pasted by user). Returns channel list for preview."""
    d    = request.get_json(force=True)
    text = d.get("text", "")
    url  = d.get("url", "")
    if url:
        try:
            text = fetch_url(url)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    channels = parse_m3u(text)
    return jsonify({"channels": channels, "count": len(channels)})

# ─── EPG source API ───────────────────────────────────────────────────────────

@app.route("/api/epg_sources")
def api_epg_list():
    with db() as c:
        return jsonify([dict(r) for r in c.execute("SELECT * FROM epg_sources ORDER BY id")])

@app.route("/api/epg_sources", methods=["POST"])
def api_epg_add():
    d = request.get_json(force=True)
    name, url = d.get("name","").strip(), d.get("url","").strip()
    if not name or not url: return jsonify({"error":"name and url required"}), 400
    with db() as c:
        c.execute("INSERT OR IGNORE INTO epg_sources (name,url) VALUES (?,?)", (name, url))
    return jsonify({"ok": True})

@app.route("/api/epg_sources/<int:sid>", methods=["DELETE"])
def api_epg_del(sid):
    with db() as c:
        c.execute("DELETE FROM epg_sources WHERE id=?", (sid,))
    return jsonify({"ok": True})

@app.route("/api/epg_sources/<int:sid>/toggle", methods=["POST"])
def api_epg_toggle(sid):
    with db() as c:
        c.execute("UPDATE epg_sources SET enabled=1-enabled WHERE id=?", (sid,))
    return jsonify({"ok": True})

@app.route("/api/epg_refresh", methods=["POST"])
def api_epg_refresh():
    threading.Thread(target=refresh_epg, daemon=True).start()
    return jsonify({"ok": True})

# ─── Guide API ───────────────────────────────────────────────────────────────

@app.route("/api/guide")
def api_guide():
    channels = read_channels()
    ts = int(time.time())
    out = []
    for ch in channels:
        name   = ch.get("name", "").strip()
        tvg_id = (ch.get("tvg_id") or slugify(name)).strip()
        n, nx  = now_next(tvg_id, ts)
        out.append({
            "name":    name,
            "group":   ch.get("group", ""),
            "tvg_id":  tvg_id,
            "logo":    ch.get("tvg_logo", ""),
            "source":  ch.get("source", ""),
            "has_url": bool(ch.get("url","").strip() and "YOUR_STREAM_URL" not in ch.get("url","")),
            "now":     n,
            "next":    nx,
        })
    return jsonify({"channels": out, "now_ts": ts})

@app.route("/api/schedule/<ch_id>")
def api_schedule(ch_id):
    ts = int(time.time())
    with db() as c:
        progs = c.execute(
            "SELECT * FROM programmes WHERE channel_id=? AND stop>? AND start<? ORDER BY start",
            (ch_id, ts - 3600, ts + 86400),
        ).fetchall()
    return jsonify({"programmes": [dict(p) for p in progs], "now_ts": ts})

@app.route("/api/status")
def api_status():
    with db() as c:
        nsrc  = c.execute("SELECT COUNT(*) FROM m3u_sources").fetchone()[0]
        nepg  = c.execute("SELECT COUNT(*) FROM epg_sources").fetchone()[0]
        nprg  = c.execute("SELECT COUNT(*) FROM programmes").fetchone()[0]
        nepgc = c.execute("SELECT COUNT(DISTINCT channel_id) FROM programmes").fetchone()[0]
        esrc  = [dict(r) for r in c.execute("SELECT * FROM epg_sources ORDER BY id")]
        msrc  = [dict(r) for r in c.execute("SELECT * FROM m3u_sources ORDER BY id")]
    return jsonify({
        "channels": len(read_channels()),
        "m3u_sources": nsrc, "epg_sources": nepg,
        "programmes": nprg, "epg_channels": nepgc,
        "epg_source_list": esrc, "m3u_source_list": msrc,
    })

# ─── Shared styles ────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#08080e;color:#d4d4e0;min-height:100vh;font-size:14px}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:#111}
::-webkit-scrollbar-thumb{background:#2a2a3e;border-radius:3px}
nav{display:flex;align-items:center;gap:2px;background:#0d0d17;
    border-bottom:1px solid #1a1a28;padding:0 20px;height:46px;
    position:sticky;top:0;z-index:60}
.nav-logo{font-weight:700;font-size:.9rem;color:#fff;margin-right:12px;
  display:flex;align-items:center;gap:7px}
.nav-logo svg{opacity:.8}
nav a.navlink{color:#6b7280;text-decoration:none;padding:5px 12px;
  border-radius:5px;font-size:.83rem;font-weight:500;transition:all .15s}
nav a.navlink:hover{background:#1a1a28;color:#d4d4e0}
nav a.navlink.active{background:#1e1e30;color:#fff}
.nav-right{margin-left:auto;display:flex;gap:6px;align-items:center}
.pill{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:12px;
  font-size:.72rem;font-weight:600;text-decoration:none;border:1px solid}
.pill-green{border-color:#16a34a;color:#4ade80;background:#0a1f0a}
.pill-blue{border-color:#1d4ed8;color:#60a5fa;background:#0a0f1f}
.content{padding:20px 24px;max-width:1440px;margin:0 auto}
h1{font-size:1.2rem;color:#fff;margin-bottom:18px;font-weight:700}
h2{font-size:.9rem;color:#fff;margin-bottom:14px;font-weight:700}
button{padding:7px 14px;border:none;border-radius:6px;cursor:pointer;
  font-size:.82rem;font-weight:600;transition:all .15s;white-space:nowrap}
.btn-blue{background:#2563eb;color:#fff}.btn-blue:hover{background:#1d4ed8}
.btn-green{background:#16a34a;color:#fff}.btn-green:hover{background:#15803d}
.btn-red{background:#dc2626;color:#fff}.btn-red:hover{background:#b91c1c}
.btn-gray{background:#1e1e2e;color:#d4d4e0;border:1px solid #2a2a3a}.btn-gray:hover{background:#2a2a3e}
.btn-amber{background:#b45309;color:#fff}.btn-amber:hover{background:#92400e}
.btn-sm{padding:3px 9px;font-size:.75rem;border-radius:5px}
input,select,textarea{background:#0f0f1a;border:1px solid #1e1e30;color:#d4d4e0;
  padding:7px 10px;border-radius:6px;font-size:.83rem;width:100%;
  transition:border-color .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:#2563eb}
input[type=checkbox]{width:auto;accent-color:#2563eb;width:15px;height:15px}
table{width:100%;border-collapse:collapse}
th{background:#0d0d17;padding:8px 10px;text-align:left;color:#4b5563;
   font-weight:600;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;
   position:sticky;top:46px;z-index:2;border-bottom:1px solid #1a1a28}
td{padding:7px 9px;border-bottom:1px solid #111120;vertical-align:middle}
tr:hover td{background:#0f0f1c}
.badge{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.7rem;font-weight:700}
.b4k{background:#5b21b6;color:#e9d5ff}
.b1080{background:#1e40af;color:#bfdbfe}
.b720{background:#0e7490;color:#a5f3fc}
.bsd{background:#1f2937;color:#9ca3af}
.notice{padding:10px 14px;border-radius:7px;font-size:.82rem;margin-bottom:14px;
  display:none;animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.notice.ok{background:#052e16;border:1px solid #16a34a;color:#4ade80}
.notice.err{background:#2d0808;border:1px solid #dc2626;color:#fca5a5}
.notice.info{background:#082040;border:1px solid #2563eb;color:#93c5fd}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.spacer{flex:1}
.card{background:#0d0d17;border:1px solid #1a1a28;border-radius:10px;padding:18px;margin-bottom:18px}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);
  z-index:200;align-items:flex-start;justify-content:center;padding-top:60px;overflow-y:auto}
.modal.open{display:flex}
.mbox{background:#0d0d17;border:1px solid #1e1e30;border-radius:12px;
  padding:24px;width:580px;max-width:96vw;margin-bottom:40px}
.mbox h2{color:#fff;margin-bottom:18px}
.frow{margin-bottom:12px}
.frow label{display:block;font-size:.76rem;color:#6b7280;margin-bottom:5px}
.frow-2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.frow-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.mbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}
.url-ok{color:#4ade80!important}.url-miss{color:#f87171!important}
input.url-ok,input.url-miss{font-family:monospace;font-size:.73rem}
.hint{font-size:.73rem;color:#374151;margin-top:4px}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:.68rem;
  background:#1a1a28;color:#6b7280;margin-left:4px}
"""

_NAV = lambda active: """
<nav>
  <span class="nav-logo">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="2" y="7" width="20" height="15" rx="2"/><polyline points="17 2 12 7 7 2"/>
    </svg>
    IPTV Manager
  </span>
  <a href="/"        class="navlink {g}">TV Guide</a>
  <a href="/channels" class="navlink {c}">Channels</a>
  <a href="/sources"  class="navlink {s}">Sources</a>
  <a href="/settings" class="navlink {st}">Settings</a>
  <span class="nav-right">
    <a class="pill pill-green" href="/playlist.m3u" target="_blank">▶ M3U</a>
    <a class="pill pill-blue"  href="/epg.xml"      target="_blank">⊞ XMLTV</a>
  </span>
</nav>
""".format(
    g ="active" if active=="guide" else "",
    c ="active" if active=="channels" else "",
    s ="active" if active=="sources" else "",
    st="active" if active=="settings" else "",
)

# ─── Guide page ───────────────────────────────────────────────────────────────

_GUIDE = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TV Guide — IPTV Manager</title><style>
""" + _CSS + """
.filter-bar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
.filter-bar input{max-width:220px}.filter-bar select{max-width:140px;width:auto}
.guide-grid{display:flex;flex-direction:column;gap:2px}
.grp-label{padding:5px 12px;font-size:.7rem;color:#2563eb;font-weight:700;
  text-transform:uppercase;letter-spacing:.08em;margin-top:8px}
.guide-row{display:grid;grid-template-columns:200px 1fr 1fr;background:#0d0d17;
  border-radius:6px;overflow:hidden;border:1px solid #111120;transition:border-color .15s;cursor:pointer}
.guide-row:hover{border-color:#1e1e30}
.ch-col{padding:10px 12px;background:#0b0b15;display:flex;flex-direction:column;gap:4px;
  justify-content:center;min-height:70px;border-right:1px solid #111120}
.ch-logo{width:28px;height:28px;object-fit:contain;border-radius:3px;margin-bottom:3px}
.ch-name{font-weight:700;font-size:.85rem;color:#fff;display:flex;align-items:center;gap:5px}
.ch-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot-ok{background:#16a34a}.dot-miss{background:#dc2626}
.ch-meta{font-size:.7rem;color:#374151}
.prog-col{padding:9px 12px;position:relative;overflow:hidden;display:flex;flex-direction:column;gap:3px}
.prog-col:first-of-type{border-right:1px solid #0f0f1c}
.prog-label{font-size:.67rem;color:#2563eb;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.prog-title{font-size:.85rem;color:#e2e2f0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prog-time{font-size:.7rem;color:#374151}
.prog-desc{font-size:.72rem;color:#4b5563;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.no-epg{color:#1f2937;font-size:.78rem;display:flex;align-items:center;padding:9px 12px}
.prog-bar{position:absolute;bottom:0;left:0;height:2px;background:linear-gradient(90deg,#2563eb,#7c3aed)}
.sp{position:fixed;right:0;top:46px;bottom:0;width:340px;background:#0d0d17;
  border-left:1px solid #1a1a28;transform:translateX(100%);transition:transform .22s;
  z-index:100;display:flex;flex-direction:column;box-shadow:-8px 0 32px rgba(0,0,0,.5)}
.sp.open{transform:translateX(0)}
.sp-hdr{padding:14px 16px;border-bottom:1px solid #1a1a28;display:flex;justify-content:space-between;align-items:center}
.sp-hdr h3{font-size:.9rem;color:#fff;font-weight:700}
.sp-body{flex:1;overflow-y:auto;padding:8px 0}
.sp-prog{padding:9px 16px;border-bottom:1px solid #0f0f1c}
.sp-prog.is-now{background:#071a07}
.sp-time{font-size:.7rem;color:#374151}
.sp-title{font-size:.83rem;color:#e2e2f0;font-weight:600;margin:2px 0}
.sp-desc{font-size:.72rem;color:#4b5563;line-height:1.4}
.now-pill{display:inline-block;background:#16a34a;color:#fff;font-size:.62rem;
  font-weight:700;padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle}
.guide-hdr{display:grid;grid-template-columns:200px 1fr 1fr;gap:2px;
  padding:0 2px;margin-bottom:5px}
.guide-hdr span{font-size:.7rem;color:#374151;padding:3px 12px;font-weight:700;text-transform:uppercase}
</style></head><body>
""" + _NAV("guide") + """
<div class="content">
<div class="filter-bar">
  <input id="q" placeholder="Search channels or programmes…" oninput="filter()">
  <select id="grp" onchange="filter()"><option value="">All groups</option></select>
  <label style="display:flex;align-items:center;gap:5px;font-size:.8rem;color:#6b7280;cursor:pointer">
    <input type="checkbox" id="only-epg" onchange="filter()"> Has schedule
  </label>
  <span id="cnt" style="color:#374151;font-size:.78rem"></span>
  <span class="spacer"></span>
  <button class="btn-gray btn-sm" onclick="load()">↻ Refresh</button>
  <span id="upd" style="color:#1f2937;font-size:.72rem"></span>
</div>
<div class="guide-hdr">
  <span>Channel</span><span>Now Playing</span><span>Up Next</span>
</div>
<div class="guide-grid" id="grid"></div>
</div>
<div class="sp" id="sp">
  <div class="sp-hdr">
    <h3 id="sp-name">Schedule</h3>
    <button class="btn-gray btn-sm" onclick="closeSp()">✕</button>
  </div>
  <div class="sp-body" id="sp-body"></div>
</div>
<script>
let all=[], nowTs=0;
function ft(ts){ return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); }
function dur(a,b){ const m=Math.round((b-a)/60); return m>=60?Math.floor(m/60)+'h'+(m%60?' '+m%60+'m':''):m+'m'; }
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function bc(r){ if(!r)return 'bsd'; r=r.toLowerCase();
  if(r==='4k'||r.includes('4k'))return 'b4k';
  if(r.includes('1080'))return 'b1080'; if(r.includes('720'))return 'b720'; return 'bsd'; }
async function load(){
  const d=await(await fetch('/api/guide')).json();
  all=d.channels; nowTs=d.now_ts;
  const gs=[...new Set(all.map(c=>c.group||'Other').filter(Boolean))].sort();
  const sel=document.getElementById('grp');
  sel.innerHTML='<option value="">All groups</option>'+gs.map(g=>`<option>${esc(g)}</option>`).join('');
  document.getElementById('upd').textContent='Updated '+new Date().toLocaleTimeString();
  filter();
}
function filter(){
  const q=document.getElementById('q').value.toLowerCase();
  const g=document.getElementById('grp').value;
  const onlyEpg=document.getElementById('only-epg').checked;
  let list=all.filter(c=>
    (!q||(c.name.toLowerCase().includes(q)||(c.now?.title||'').toLowerCase().includes(q))) &&
    (!g||c.group===g) &&
    (!onlyEpg||c.now||c.next)
  );
  document.getElementById('cnt').textContent=list.length+' channels';
  const groups=[...new Set(list.map(c=>c.group||'Other'))];
  let html='';
  for(const grp of groups){
    const chs=list.filter(c=>(c.group||'Other')===grp);
    html+=`<div class="grp-label">${esc(grp)} <span style="color:#1f2937;font-weight:400">${chs.length}</span></div>`;
    for(const ch of chs) html+=row(ch);
  }
  document.getElementById('grid').innerHTML=html;
}
function row(ch){
  const dot=ch.has_url?'dot-ok':'dot-miss';
  let logo=ch.logo?`<img class="ch-logo" src="${esc(ch.logo)}" onerror="this.style.display='none'">`:'';
  const src=ch.source?`<span class="tag">${esc(ch.source)}</span>`:'';
  const chCol=`<div class="ch-col">${logo}<div class="ch-name"><span class="ch-dot ${dot}"></span>${esc(ch.name)}${src}</div><div class="ch-meta">${esc(ch.group)}</div></div>`;
  return `<div class="guide-row" onclick="openSp('${esc(ch.tvg_id)}','${esc(ch.name)}')">${chCol}${pc(ch.now,'NOW',true)}${pc(ch.next,'NEXT',false)}</div>`;
}
function pc(p,lbl,bar){
  if(!p) return `<div class="no-epg">No EPG — set TVG-ID + add XMLTV source</div>`;
  let barHtml='';
  if(bar&&p){const pct=Math.min(100,Math.max(0,Math.round((nowTs-p.start)/(p.stop-p.start)*100)));
    barHtml=`<div class="prog-bar" style="width:${pct}%"></div>`;}
  return `<div class="prog-col">
    <div class="prog-label">${lbl}</div>
    <div class="prog-title">${esc(p.title||'?')}</div>
    <div class="prog-time">${ft(p.start)} – ${ft(p.stop)} · ${dur(p.start,p.stop)}</div>
    ${p.desc?`<div class="prog-desc">${esc(p.desc)}</div>`:''}${barHtml}</div>`;
}
async function openSp(id,name){
  document.getElementById('sp-name').textContent=name;
  document.getElementById('sp-body').innerHTML='<div style="padding:20px;color:#374151">Loading…</div>';
  document.getElementById('sp').classList.add('open');
  const d=await(await fetch('/api/schedule/'+encodeURIComponent(id))).json();
  if(!d.programmes.length){
    document.getElementById('sp-body').innerHTML='<div style="padding:20px;color:#1f2937">No schedule data.<br>Set TVG-ID to match your XMLTV source.</div>';
    return;
  }
  const now=d.now_ts;
  document.getElementById('sp-body').innerHTML=d.programmes.map(p=>{
    const isNow=p.start<=now&&p.stop>now;
    return `<div class="sp-prog${isNow?' is-now':''}">
      <div class="sp-time">${ft(p.start)} – ${ft(p.stop)}${isNow?'<span class="now-pill">NOW</span>':''}</div>
      <div class="sp-title">${esc(p.title||'?')}</div>
      ${p.desc?`<div class="sp-desc">${esc((p.desc||'').substring(0,140))}</div>`:''}
    </div>`;
  }).join('');
  setTimeout(()=>{ const n=document.querySelector('.sp-prog.is-now'); if(n)n.scrollIntoView({block:'center'}); },80);
}
function closeSp(){ document.getElementById('sp').classList.remove('open'); }
load(); setInterval(load,60000);
</script></body></html>"""

# ─── Channels page ────────────────────────────────────────────────────────────

_CHANNELS = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Channels — IPTV Manager</title><style>
""" + _CSS + """
.url-cell input{font-family:monospace;font-size:.72rem}
.sel-col{width:32px;text-align:center}
</style></head><body>
""" + _NAV("channels") + """
<div class="content">
<div class="toolbar">
  <button class="btn-blue" onclick="openAdd()">+ Add Channel</button>
  <button class="btn-gray" onclick="openBulkImport()">⇩ Paste M3U</button>
  <input style="max-width:200px" placeholder="Search…" oninput="filter(this.value)" id="q">
  <select id="gf" onchange="filter(document.getElementById('q').value)" style="max-width:130px;width:auto">
    <option value="">All groups</option>
  </select>
  <span id="cnt" style="color:#374151;font-size:.78rem"></span>
  <span class="spacer"></span>
  <button class="btn-red btn-sm" id="bulk-del-btn" onclick="bulkDelete()" style="display:none">Delete selected</button>
  <button class="btn-gray btn-sm" onclick="exportCsv()">↓ CSV</button>
</div>
<div class="notice" id="notice"></div>
<table>
<thead><tr>
  <th class="sel-col"><input type="checkbox" id="chk-all" onchange="toggleAll(this.checked)"></th>
  <th style="width:28px">#</th>
  <th>Name</th><th>Group</th><th>Res</th>
  <th>TVG-ID</th>
  <th>Stream URL</th>
  <th style="width:88px"></th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>

<!-- Add/Edit modal -->
<div class="modal" id="modal">
<div class="mbox">
  <h2 id="mtitle">Add Channel</h2>
  <div class="frow"><label>Name *</label><input id="f-name" placeholder="ESPN+"></div>
  <div class="frow-2">
    <div class="frow"><label>Group</label><input id="f-group" value="Sports"></div>
    <div class="frow"><label>Source tag</label><input id="f-source" placeholder="optional"></div>
  </div>
  <div class="frow-3">
    <div class="frow"><label>Resolution</label>
      <select id="f-resolution"><option value="">—</option><option value="4k">4K</option>
        <option value="1080p">1080p</option><option value="720p">720p</option>
        <option value="480p">480p</option><option value="360p">360p</option></select>
    </div>
    <div class="frow"><label>FPS</label>
      <select id="f-fps"><option value="">—</option><option value="60">60</option>
        <option value="30">30</option><option value="25">25</option></select>
    </div>
    <div class="frow"><label>Bitrate kbps</label><input id="f-bitrate" type="number" placeholder="4500"></div>
  </div>
  <div class="frow">
    <label>TVG-ID <span class="hint" style="display:inline">(match your XMLTV channel ID for EPG)</span></label>
    <input id="f-tvg_id" placeholder="espn.us">
  </div>
  <div class="frow"><label>Logo URL</label><input id="f-tvg_logo" placeholder="https://…/logo.png"></div>
  <div class="frow"><label>Stream URL</label><input id="f-url" placeholder="https://host/stream.m3u8"></div>
  <div class="mbtns">
    <button class="btn-gray" onclick="closeM()">Cancel</button>
    <button class="btn-green" onclick="save()">Save Channel</button>
  </div>
</div>
</div>

<!-- Paste M3U modal -->
<div class="modal" id="paste-modal">
<div class="mbox" style="width:700px">
  <h2>Paste M3U Text</h2>
  <p style="color:#6b7280;font-size:.8rem;margin-bottom:12px">
    Paste raw M3U content below to preview and import channels. URLs and metadata are auto-parsed.
  </p>
  <div class="frow">
    <textarea id="paste-text" rows="8" placeholder="#EXTM3U&#10;#EXTINF:-1 tvg-id=&quot;espn.us&quot; group-title=&quot;Sports&quot;,ESPN&#10;http://stream.url/espn.m3u8" style="font-family:monospace;font-size:.75rem;resize:vertical"></textarea>
  </div>
  <div class="mbtns">
    <button class="btn-gray" onclick="closePaste()">Cancel</button>
    <button class="btn-blue" onclick="parsePaste()">Preview Channels →</button>
  </div>
</div>
</div>

<!-- Import preview modal -->
<div class="modal" id="preview-modal">
<div class="mbox" style="width:820px;max-height:90vh;display:flex;flex-direction:column">
  <h2 id="preview-title">Preview Import</h2>
  <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center">
    <button class="btn-gray btn-sm" onclick="selAll(true)">Select all</button>
    <button class="btn-gray btn-sm" onclick="selAll(false)">Deselect all</button>
    <input id="pv-search" placeholder="Filter…" style="max-width:160px" oninput="renderPreview()">
    <select id="pv-grp" onchange="renderPreview()" style="max-width:140px;width:auto"><option value="">All groups</option></select>
    <span id="pv-cnt" style="color:#6b7280;font-size:.78rem"></span>
    <label style="display:flex;align-items:center;gap:5px;font-size:.78rem;color:#6b7280;cursor:pointer">
      <input type="checkbox" id="pv-new-only" onchange="renderPreview()"> New only
    </label>
    <span class="spacer"></span>
    <button class="btn-green" onclick="doImport()">Import Selected</button>
    <button class="btn-gray" onclick="closePreview()">Cancel</button>
  </div>
  <div style="overflow-y:auto;flex:1;border:1px solid #1a1a28;border-radius:6px">
    <table style="font-size:.78rem">
      <thead><tr>
        <th style="width:32px"><input type="checkbox" id="pv-all" onchange="selAll(this.checked)"></th>
        <th>Name</th><th>Group</th><th>Res</th><th>TVG-ID</th><th>URL</th>
      </tr></thead>
      <tbody id="pv-body"></tbody>
    </table>
  </div>
</div>
</div>

<script>
let channels=[], editIdx=null, filterQ='', filterG='';
let previewData=[], previewSel=new Set(), existingUrls=new Set(), importSrcId=null;

function bc(r){if(!r)return 'bsd';r=r.toLowerCase();
  if(r==='4k'||r.includes('4k'))return 'b4k';
  if(r.includes('1080'))return 'b1080';if(r.includes('720'))return 'b720';return 'bsd';}
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function notify(msg,type='ok'){
  const el=document.getElementById('notice');
  el.textContent=msg;el.className='notice '+type;el.style.display='block';
  clearTimeout(notify._t);notify._t=setTimeout(()=>el.style.display='none',4000);
}

async function load(){
  channels=await(await fetch('/api/channels')).json();
  existingUrls=new Set(channels.map(c=>(c.url||'').trim()).filter(Boolean));
  const gs=[...new Set(channels.map(c=>c.group||'').filter(Boolean))].sort();
  const sel=document.getElementById('gf');
  sel.innerHTML='<option value="">All groups</option>'+gs.map(g=>`<option>${esc(g)}</option>`).join('');
  render();
}
function filter(q){filterQ=(q||'').toLowerCase();filterG=document.getElementById('gf').value;render();}
function render(){
  const list=channels.filter(c=>
    (!filterQ||c.name.toLowerCase().includes(filterQ)||
     (c.url||'').toLowerCase().includes(filterQ)) &&
    (!filterG||c.group===filterG)
  );
  document.getElementById('cnt').textContent=channels.length+' channels';
  document.getElementById('tbody').innerHTML=list.map(c=>{
    const i=channels.indexOf(c);
    const hasUrl=c.url&&!c.url.includes('YOUR_STREAM_URL');
    return `<tr>
      <td class="sel-col"><input type="checkbox" class="row-chk" data-idx="${i}" onchange="updateBulkBtn()"></td>
      <td style="color:#374151">${i+1}</td>
      <td><strong>${esc(c.name)}</strong>${c.source?`<span class="tag">${esc(c.source)}</span>`:''}</td>
      <td style="color:#4b5563">${esc(c.group)}</td>
      <td><span class="badge ${bc(c.resolution)}">${esc(c.resolution)||'—'}</span></td>
      <td style="font-family:monospace;font-size:.72rem;color:#374151">${esc(c.tvg_id)||'<span style="color:#1f2937">—</span>'}</td>
      <td><input class="url-cell ${hasUrl?'url-ok':'url-miss'}" value="${esc(c.url)}"
           placeholder="https://…" onchange="quickUrl(${i},this.value)"></td>
      <td style="white-space:nowrap">
        <button class="btn-blue btn-sm" style="margin-right:3px" onclick="openEdit(${i})">Edit</button>
        <button class="btn-red btn-sm" onclick="del(${i})">✕</button>
      </td>
    </tr>`;
  }).join('');
}
function toggleAll(v){ document.querySelectorAll('.row-chk').forEach(c=>c.checked=v); updateBulkBtn(); }
function updateBulkBtn(){
  const any=[...document.querySelectorAll('.row-chk')].some(c=>c.checked);
  document.getElementById('bulk-del-btn').style.display=any?'':'none';
}
async function bulkDelete(){
  const idxs=[...document.querySelectorAll('.row-chk:checked')].map(c=>+c.dataset.idx);
  if(!idxs.length||!confirm('Delete '+idxs.length+' channels?'))return;
  await fetch('/api/channels/bulk_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({indices:idxs})});
  notify('Deleted '+idxs.length+' channels');load();
}
async function quickUrl(idx,url){
  await fetch('/api/channels/'+idx,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
  channels[idx].url=url;notify('Saved URL for '+channels[idx].name);render();
}
function openAdd(){
  editIdx=null;document.getElementById('mtitle').textContent='Add Channel';
  ['name','tvg_id','tvg_logo','url','bitrate','source'].forEach(f=>document.getElementById('f-'+f).value='');
  document.getElementById('f-group').value='Sports';
  document.getElementById('f-resolution').value='1080p';
  document.getElementById('f-fps').value='60';
  document.getElementById('modal').classList.add('open');
}
function openEdit(idx){
  editIdx=idx;const c=channels[idx];
  document.getElementById('mtitle').textContent='Edit: '+c.name;
  ['name','group','resolution','fps','bitrate','tvg_id','tvg_logo','url','source'].forEach(f=>
    document.getElementById('f-'+f).value=c[f]||'');
  document.getElementById('modal').classList.add('open');
}
function closeM(){document.getElementById('modal').classList.remove('open');}
async function save(){
  const d={};
  ['name','group','resolution','fps','bitrate','tvg_id','tvg_logo','url','source'].forEach(f=>
    d[f]=(document.getElementById('f-'+f).value||'').trim());
  if(!d.name){alert('Name required');return;}
  if(editIdx!==null){
    await fetch('/api/channels/'+editIdx,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
    notify('Updated: '+d.name);
  } else {
    await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
    notify('Added: '+d.name);
  }
  closeM();load();
}
async function del(idx){
  if(!confirm('Delete "'+channels[idx].name+'"?'))return;
  await fetch('/api/channels/'+idx,{method:'DELETE'});notify('Deleted');load();
}
function exportCsv(){window.open('/api/channels/export.csv');}

// ── Paste M3U import ──
function openBulkImport(){document.getElementById('paste-text').value='';document.getElementById('paste-modal').classList.add('open');}
function closePaste(){document.getElementById('paste-modal').classList.remove('open');}
async function parsePaste(){
  const text=document.getElementById('paste-text').value.trim();
  if(!text){alert('Paste some M3U content first');return;}
  const r=await fetch('/api/sources/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  const d=await r.json();
  if(d.error){notify(d.error,'err');return;}
  closePaste();
  importSrcId=null;
  openPreview(d.channels,'Paste Import — '+d.count+' channels found');
}
function openPreview(data,title){
  previewData=data;
  previewSel=new Set(data.map((_,i)=>i));
  importSrcId=null;
  document.getElementById('preview-title').textContent=title;
  const gs=[...new Set(data.map(c=>c.group||'').filter(Boolean))].sort();
  document.getElementById('pv-grp').innerHTML='<option value="">All groups</option>'+gs.map(g=>`<option>${esc(g)}</option>`).join('');
  document.getElementById('preview-modal').classList.add('open');
  renderPreview();
}
function closePreview(){document.getElementById('preview-modal').classList.remove('open');}
function selAll(v){previewData.forEach((_,i)=>v?previewSel.add(i):previewSel.delete(i));renderPreview();}
function renderPreview(){
  const q=(document.getElementById('pv-search').value||'').toLowerCase();
  const g=document.getElementById('pv-grp').value;
  const newOnly=document.getElementById('pv-new-only').checked;
  const list=previewData.filter((c,i)=>
    (!q||c.name.toLowerCase().includes(q)||(c.group||'').toLowerCase().includes(q))&&
    (!g||c.group===g)&&
    (!newOnly||!existingUrls.has((c.url||'').trim()))
  );
  document.getElementById('pv-cnt').textContent=previewSel.size+' of '+previewData.length+' selected';
  document.getElementById('pv-body').innerHTML=list.map(c=>{
    const i=previewData.indexOf(c);
    const isNew=!existingUrls.has((c.url||'').trim());
    return `<tr style="${isNew?'':'opacity:.45'}">
      <td style="text-align:center"><input type="checkbox" ${previewSel.has(i)?'checked':''} onchange="togSel(${i},this.checked)"></td>
      <td>${esc(c.name)}${isNew?'':' <span style="color:#374151;font-size:.68rem">(exists)</span>'}</td>
      <td style="color:#4b5563">${esc(c.group)}</td>
      <td><span class="badge ${bc(c.resolution)}">${esc(c.resolution)||'—'}</span></td>
      <td style="font-family:monospace;font-size:.7rem;color:#374151">${esc(c.tvg_id)||'—'}</td>
      <td style="font-family:monospace;font-size:.7rem;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#4b5563" title="${esc(c.url)}">${esc(c.url)}</td>
    </tr>`;
  }).join('');
}
function togSel(i,v){v?previewSel.add(i):previewSel.delete(i);renderPreview();}
async function doImport(){
  const selected=previewData.filter((_,i)=>previewSel.has(i));
  if(!selected.length){alert('Nothing selected');return;}
  let res;
  if(importSrcId!==null){
    res=await(await fetch('/api/sources/'+importSrcId+'/import',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({channels:selected})})).json();
  } else {
    // paste import — add directly
    let added=0;
    for(const ch of selected){
      if(existingUrls.has((ch.url||'').trim()))continue;
      await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ch)});
      added++;
    }
    res={ok:true,added};
  }
  closePreview();
  notify('Imported '+res.added+' channels');
  load();
}

// expose for sources page to call
window._openPreviewForSource=(channels,title,srcId)=>{
  importSrcId=srcId; openPreview(channels,title);
};
load();
</script></body></html>"""

# ─── Sources page ─────────────────────────────────────────────────────────────

_SOURCES = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sources — IPTV Manager</title><style>
""" + _CSS + """
.src-card{background:#0d0d17;border:1px solid #1a1a28;border-radius:10px;
  padding:16px 18px;margin-bottom:10px;display:flex;align-items:center;gap:14px;transition:border-color .15s}
.src-card:hover{border-color:#1e1e30}
.src-icon{width:36px;height:36px;border-radius:8px;background:#0f0f1a;display:flex;
  align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0}
.src-info{flex:1;min-width:0}
.src-name{font-weight:700;font-size:.9rem;color:#fff}
.src-url{font-family:monospace;font-size:.72rem;color:#374151;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;max-width:500px;margin-top:2px}
.src-meta{font-size:.72rem;color:#374151;margin-top:3px;display:flex;gap:10px;flex-wrap:wrap}
.src-meta span{display:flex;align-items:center;gap:3px}
.src-actions{display:flex;gap:6px;align-items:center;flex-shrink:0}
.src-enabled{border-color:#16a34a!important}.src-disabled{opacity:.5}
.status-ok{color:#4ade80}.status-err{color:#f87171}.status-pending{color:#60a5fa}
.add-card{background:#0a0a12;border:1px dashed #1a1a28;border-radius:10px;padding:20px}
.add-card h2{margin-bottom:14px}
.add-grid{display:grid;grid-template-columns:1fr 2fr 1fr auto;gap:10px;align-items:end}
.empty{text-align:center;padding:60px 20px;color:#1f2937}
.empty-icon{font-size:3rem;margin-bottom:12px}
.sync-progress{display:none;padding:10px 14px;background:#082040;border:1px solid #1e3a6e;
  border-radius:6px;font-size:.82rem;color:#93c5fd;margin-bottom:12px}
</style></head><body>
""" + _NAV("sources") + """
<div class="content">
<h1>M3U Sources</h1>
<p style="color:#4b5563;font-size:.83rem;margin-bottom:20px">
  Add any M3U playlist URL. Sync it to preview and import channels — all metadata
  (name, group, TVG-ID, logo, resolution) is parsed automatically.
</p>
<div class="notice" id="notice"></div>
<div class="sync-progress" id="sync-prog"></div>
<div id="src-list"></div>

<div class="add-card">
  <h2>Add M3U Source</h2>
  <div class="add-grid">
    <div class="frow" style="margin:0">
      <label>Name</label>
      <input id="new-name" placeholder="My Sports Playlist">
    </div>
    <div class="frow" style="margin:0">
      <label>M3U URL</label>
      <input id="new-url" placeholder="https://example.com/playlist.m3u" type="url">
    </div>
    <div class="frow" style="margin:0">
      <label>Auto-sync</label>
      <select id="new-auto">
        <option value="0">Manual only</option>
        <option value="1">Every 12 hours</option>
      </select>
    </div>
    <button class="btn-blue" onclick="addSource()" style="margin-top:20px">Add Source</button>
  </div>
  <p class="hint" style="margin-top:10px">
    Works with any standard M3U/M3U8 URL. After adding, click <strong>Sync</strong> to fetch and preview channels.
    Gzipped playlists (.m3u.gz) are supported automatically.
  </p>
</div>
</div>

<script>
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function age(ts){if(!ts)return'Never';const s=Math.floor(Date.now()/1000)-ts;
  if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
function notify(msg,type='ok'){
  const el=document.getElementById('notice');
  el.textContent=msg;el.className='notice '+type;el.style.display='block';
  clearTimeout(notify._t);notify._t=setTimeout(()=>el.style.display='none',5000);}

async function load(){
  const sources=await(await fetch('/api/sources')).json();
  const el=document.getElementById('src-list');
  if(!sources.length){
    el.innerHTML=`<div class="empty"><div class="empty-icon">📡</div>
      <p>No sources yet.</p><p style="margin-top:6px;font-size:.82rem">Add an M3U URL above to get started.</p></div>`;
    return;
  }
  el.innerHTML=sources.map(s=>{
    const cls=s.enabled?'src-enabled':'src-disabled';
    const st=s.last_error?`<span class="status-err">⚠ ${esc(s.last_error.substring(0,60))}</span>`:
             s.last_count?`<span class="status-ok">✓ ${s.last_count.toLocaleString()} channels</span>`:
             `<span class="status-pending">Not synced yet</span>`;
    return `<div class="src-card ${cls}">
      <div class="src-icon">📡</div>
      <div class="src-info">
        <div class="src-name">${esc(s.name)}</div>
        <div class="src-url" title="${esc(s.url)}">${esc(s.url)}</div>
        <div class="src-meta">
          ${st}
          <span>🕐 ${age(s.last_synced)}</span>
          ${s.auto_sync?'<span>🔄 Auto-sync</span>':''}
        </div>
      </div>
      <div class="src-actions">
        <button class="btn-blue btn-sm" onclick="sync(${s.id},'${esc(s.name)}')">⟳ Sync</button>
        <button class="btn-gray btn-sm" onclick="toggleSrc(${s.id})">${s.enabled?'Disable':'Enable'}</button>
        <button class="btn-red btn-sm" onclick="delSrc(${s.id},'${esc(s.name)}')">✕</button>
      </div>
    </div>`;
  }).join('');
}

async function addSource(){
  const name=document.getElementById('new-name').value.trim();
  const url=document.getElementById('new-url').value.trim();
  const auto=document.getElementById('new-auto').value;
  if(!name||!url){alert('Name and URL are required');return;}
  const r=await fetch('/api/sources',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name,url,auto_sync:+auto})});
  const d=await r.json();
  if(d.error){notify(d.error,'err');return;}
  document.getElementById('new-name').value='';
  document.getElementById('new-url').value='';
  notify('Source added — click Sync to fetch channels','info');
  load();
}

async function sync(id,name){
  const prog=document.getElementById('sync-prog');
  prog.textContent='⟳ Fetching '+name+'…';prog.style.display='block';
  notify('Fetching '+name+'…','info');
  const r=await fetch('/api/sources/'+id+'/sync');
  prog.style.display='none';
  const d=await r.json();
  if(d.error){notify('Error: '+d.error,'err');return;}
  notify('Fetched '+d.count+' channels — previewing…','info');
  load();
  // Open preview on channels page — navigate and open
  sessionStorage.setItem('import_preview',JSON.stringify({channels:d.channels,title:name+' — '+d.count+' channels',srcId:id}));
  window.location.href='/channels?import=1';
}

async function toggleSrc(id){ await fetch('/api/sources/'+id+'/toggle',{method:'POST'}); load(); }
async function delSrc(id,name){
  if(!confirm('Remove source "'+name+'"?\n\nImported channels are NOT deleted.'))return;
  await fetch('/api/sources/'+id,{method:'DELETE'}); notify('Removed'); load();
}
load();
</script></body></html>"""

# ─── Settings page ────────────────────────────────────────────────────────────

_SETTINGS = """<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings — IPTV Manager</title><style>
""" + _CSS + """
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}
.stat-box{background:#0d0d17;border:1px solid #1a1a28;border-radius:8px;padding:14px;text-align:center}
.stat-n{font-size:1.7rem;font-weight:800;color:#2563eb}
.stat-l{font-size:.72rem;color:#374151;margin-top:3px}
.epg-row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #111120}
.epg-row:last-child{border:none}
.epg-name{font-weight:600;font-size:.85rem;flex:0 0 150px;color:#d4d4e0}
.epg-url{font-family:monospace;font-size:.72rem;color:#374151;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.epg-meta{font-size:.72rem;color:#374151;flex:0 0 130px;text-align:right;line-height:1.5}
.epg-actions{display:flex;gap:6px;flex:0 0 auto}
.add-epg{display:grid;grid-template-columns:1fr 2fr auto;gap:10px;align-items:end;margin-top:14px}
.tip{background:#082040;border:1px solid #1e3a6e;border-radius:8px;padding:14px 16px;
  font-size:.8rem;color:#93c5fd;line-height:1.8;margin-top:14px}
.tip code{background:#040810;padding:2px 6px;border-radius:3px;font-size:.75rem;color:#60a5fa}
.endpoint-box{background:#040810;border:1px solid #1a1a28;border-radius:6px;padding:12px 14px;
  font-family:monospace;font-size:.78rem;color:#60a5fa;margin-bottom:8px}
</style></head><body>
""" + _NAV("settings") + """
<div class="content">
<div class="stat-grid" id="stats"></div>

<div class="card">
  <h2>Jellyfin Configuration</h2>
  <p style="color:#4b5563;font-size:.8rem;margin-bottom:12px">Add these in Jellyfin → Dashboard → Live TV:</p>
  <p style="font-size:.78rem;color:#6b7280;margin-bottom:4px">M3U Tuner URL:</p>
  <div class="endpoint-box" id="m3u-url">http://10.0.10.98:8765/playlist.m3u</div>
  <p style="font-size:.78rem;color:#6b7280;margin-bottom:4px;margin-top:10px">XMLTV Guide URL <span style="color:#374151">(auto-embedded in M3U header — Jellyfin picks it up automatically)</span>:</p>
  <div class="endpoint-box" id="xmltv-url">http://10.0.10.98:8765/epg.xml</div>
  <div style="display:flex;gap:8px;margin-top:12px">
    <a href="/playlist.m3u" target="_blank"><button class="btn-green btn-sm">▶ View M3U</button></a>
    <a href="/epg.xml" target="_blank"><button class="btn-blue btn-sm">⊞ View XMLTV</button></a>
  </div>
</div>

<div class="card">
  <h2>EPG Sources (XMLTV)</h2>
  <div id="epg-list"></div>
  <div class="add-epg">
    <div class="frow" style="margin:0"><label>Name</label><input id="en" placeholder="US Guide"></div>
    <div class="frow" style="margin:0"><label>XMLTV URL</label><input id="eu" placeholder="https://…/guide.xml" type="url"></div>
    <button class="btn-blue" onclick="addEpg()" style="margin-top:20px">Add</button>
  </div>
  <div class="tip">
    <strong>How EPG works:</strong><br>
    1. Add an XMLTV source URL (search GitHub for "iptv-org epg" or "free xmltv &lt;country&gt;").<br>
    2. In <strong>Channels</strong>, set each channel's <code>TVG-ID</code> to the channel ID in that XMLTV file.<br>
    3. Click <strong>Refresh EPG</strong> — data appears in the TV Guide within seconds.<br><br>
    <strong>Free XMLTV sources to try:</strong><br>
    • <code>https://epg.streamstv.me/epg/guide-usa.xml</code> — US<br>
    • <code>https://epg.streamstv.me/epg/guide-canada.xml</code> — Canada<br>
    • <code>https://epg.streamstv.me/epg/guide-uk.xml</code> — UK<br>
    • Search: <code>github.com/iptv-org/epg</code> for country-specific guides
  </div>
</div>

<div class="card">
  <h2>Actions</h2>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <button class="btn-green" onclick="refreshEpg()">↻ Refresh EPG Now</button>
    <button class="btn-gray" onclick="loadStatus()">↻ Reload Status</button>
  </div>
  <p style="margin-top:10px;font-size:.78rem;color:#374151">
    EPG auto-refreshes every 12 hours. Programmes kept for 3 days.
  </p>
</div>
</div>

<div class="notice" id="notice"></div>

<script>
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function age(ts){if(!ts)return'Never';const s=Math.floor(Date.now()/1000)-ts;
  if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
function notify(msg,type='ok'){
  const el=document.getElementById('notice');
  el.textContent=msg;el.className='notice '+type;el.style.display='block';
  clearTimeout(notify._t);notify._t=setTimeout(()=>el.style.display='none',5000);}

async function loadStatus(){
  const d=await(await fetch('/api/status')).json();
  document.getElementById('stats').innerHTML=`
    <div class="stat-box"><div class="stat-n">${d.channels}</div><div class="stat-l">Channels</div></div>
    <div class="stat-box"><div class="stat-n">${d.m3u_sources}</div><div class="stat-l">M3U Sources</div></div>
    <div class="stat-box"><div class="stat-n">${d.epg_sources}</div><div class="stat-l">EPG Sources</div></div>
    <div class="stat-box"><div class="stat-n">${d.epg_channels}</div><div class="stat-l">EPG Channels</div></div>
    <div class="stat-box"><div class="stat-n">${d.programmes.toLocaleString()}</div><div class="stat-l">Programmes</div></div>
  `;
  // update endpoint URLs with actual host
  const base=window.location.protocol+'//'+window.location.hostname+':8765';
  document.getElementById('m3u-url').textContent=base+'/playlist.m3u';
  document.getElementById('xmltv-url').textContent=base+'/epg.xml';
}
async function loadEpg(){
  const sources=await(await fetch('/api/epg_sources')).json();
  const el=document.getElementById('epg-list');
  if(!sources.length){
    el.innerHTML='<p style="color:#374151;font-size:.82rem;padding:8px 0">No EPG sources yet. Add one below.</p>';return;}
  el.innerHTML=sources.map(s=>`
    <div class="epg-row">
      <span class="epg-name ${s.enabled?'':''}">
        ${s.enabled?'<span style="color:#16a34a">●</span>':'<span style="color:#374151">●</span>'}
        ${esc(s.name)}
      </span>
      <span class="epg-url" title="${esc(s.url)}">${esc(s.url)}</span>
      <span class="epg-meta">
        ${s.last_count?s.last_count.toLocaleString()+' progs<br>':''}
        ${age(s.last_fetched)}
        ${s.last_error?'<br><span style="color:#f87171">'+esc(s.last_error.substring(0,40))+'</span>':''}
      </span>
      <div class="epg-actions">
        <button class="btn-gray btn-sm" onclick="toggleEpg(${s.id})">${s.enabled?'Off':'On'}</button>
        <button class="btn-red btn-sm" onclick="delEpg(${s.id})">✕</button>
      </div>
    </div>`).join('');
}
async function addEpg(){
  const name=document.getElementById('en').value.trim();
  const url=document.getElementById('eu').value.trim();
  if(!name||!url){alert('Name and URL required');return;}
  await fetch('/api/epg_sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,url})});
  document.getElementById('en').value='';document.getElementById('eu').value='';
  notify('EPG source added — click Refresh EPG Now','info');loadEpg();loadStatus();
}
async function delEpg(id){ await fetch('/api/epg_sources/'+id,{method:'DELETE'}); notify('Removed'); loadEpg();loadStatus(); }
async function toggleEpg(id){ await fetch('/api/epg_sources/'+id+'/toggle',{method:'POST'}); loadEpg(); }
async function refreshEpg(){
  notify('Refreshing EPG in background…','info');
  await fetch('/api/epg_refresh',{method:'POST'});
  setTimeout(()=>{loadStatus();loadEpg();},5000);
}
loadStatus();loadEpg();
</script></body></html>"""

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def page_guide():    return Response(_GUIDE,    mimetype="text/html")

@app.route("/channels")
def page_channels():
    # detect redirect from sources page with preview data
    imp = request.args.get("import")
    page = _CHANNELS
    if imp:
        page = _CHANNELS.replace(
            "load();",
            """load();
// Auto-open import preview from sources page
const _prev=sessionStorage.getItem('import_preview');
if(_prev){sessionStorage.removeItem('import_preview');
  const d=JSON.parse(_prev);
  setTimeout(()=>window._openPreviewForSource(d.channels,d.title,d.srcId),400);}"""
        )
    return Response(page, mimetype="text/html")

@app.route("/sources")
def page_sources():  return Response(_SOURCES,  mimetype="text/html")

@app.route("/settings")
def page_settings(): return Response(_SETTINGS, mimetype="text/html")

# ─── Boot ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    threading.Thread(target=_epg_loop,              daemon=True).start()
    threading.Thread(target=_source_autosync_loop,  daemon=True).start()
    host = os.environ.get("SERVER_HOST", "localhost")
    print("[IPTV Manager] http://{}:8765".format(host))
    print("  M3U:   http://{}:8765/playlist.m3u".format(host))
    print("  XMLTV: http://{}:8765/epg.xml".format(host))
    app.run(host="0.0.0.0", port=8765, debug=False, threaded=True)
