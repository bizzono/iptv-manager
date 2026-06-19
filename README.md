# IPTV Manager

Self-hosted IPTV playlist manager with TV Guide, EPG schedule, and one-click M3U source import. Runs on any machine with Python 3.9+. Designed for use with Jellyfin but works with any IPTV client.

## Features

- **TV Guide** — Now/next for every channel with live progress bar. Click any channel for a full-day schedule panel.
- **M3U Source Import** — Paste any M3U URL, preview all channels with metadata pre-parsed, select what to import. Handles gzip automatically.
- **EPG / XMLTV** — Add any XMLTV guide source. Programme data cached in SQLite, served at `/epg.xml`. Auto-refreshes every 12 hours.
- **Channel Manager** — Add/edit/delete channels, paste stream URLs inline, bulk delete, export to CSV.
- **Live M3U output** — `/playlist.m3u` generates dynamically from your channel list on every request. No static files, no cron jobs.
- **Jellyfin-ready** — M3U header embeds the XMLTV guide URL automatically. Jellyfin picks it up with zero extra config.

## Quick Start (Mac / Linux)

```bash
git clone https://github.com/bizzono/iptv-manager
cd iptv-manager
bash install.sh
```

Open `http://localhost:8765`

## Manual Install

```bash
pip install flask
python iptv_manager.py
```

## Docker

```bash
docker compose up -d
```

Set `SERVER_HOST` in `.env` to your machine's LAN IP so the M3U header points to the right address.

## Jellyfin Setup

In Jellyfin → Dashboard → Live TV:

| Field | Value |
|---|---|
| Tuner type | M3U Tuner |
| M3U URL | `http://<your-ip>:8765/playlist.m3u` |
| Guide type | XMLTV |
| XMLTV URL | `http://<your-ip>:8765/epg.xml` |

## Adding Channels

### Option 1 — Import from M3U URL (Sources page)
1. Go to **Sources** → Add a source URL
2. Click **⟳ Sync** → preview all channels found
3. Select what you want → **Import Selected**

### Option 2 — Paste M3U text (Channels page)
1. Go to **Channels** → **⇩ Paste M3U**
2. Paste raw M3U content → **Preview Channels**
3. Select and import

### Option 3 — Manual (Channels page)
Click **+ Add Channel** and fill in the form.

## Getting EPG (TV Schedule Data)

1. Go to **Settings** → **EPG Sources** → add an XMLTV URL
2. Click **↻ Refresh EPG Now**
3. In **Channels**, set each channel's **TVG-ID** to match the channel ID in your XMLTV file
4. TV Guide populates automatically

Free XMLTV sources:
- `https://epg.streamstv.me/epg/guide-usa.xml` — US
- `https://epg.streamstv.me/epg/guide-canada.xml` — Canada  
- `https://epg.streamstv.me/epg/guide-uk.xml` — UK
- [github.com/iptv-org/epg](https://github.com/iptv-org/epg) — country-specific guides

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/playlist.m3u` | Live M3U playlist |
| GET | `/epg.xml` | XMLTV guide output |
| GET | `/api/channels` | List channels (JSON) |
| POST | `/api/channels` | Add channel |
| PUT | `/api/channels/<idx>` | Update channel |
| DELETE | `/api/channels/<idx>` | Delete channel |
| GET | `/api/channels/export.csv` | Export channels as CSV |
| GET | `/api/guide` | Now/next for all channels |
| GET | `/api/schedule/<tvg_id>` | Full day schedule for a channel |
| GET | `/api/sources` | List M3U sources |
| POST | `/api/sources` | Add M3U source |
| GET | `/api/sources/<id>/sync` | Fetch + parse source, return preview |
| POST | `/api/sources/<id>/import` | Import selected channels from source |
| POST | `/api/sources/parse` | Parse raw M3U text, return preview |
| POST | `/api/epg_refresh` | Trigger EPG refresh |
| GET | `/api/status` | Server status |

## File Structure

```
iptv_manager.py     Main app (single file, no build step)
channels.csv        Channel data — edit directly or via UI
iptv.db             SQLite — EPG cache + source metadata (auto-created)
requirements.txt    Just: flask
Dockerfile
docker-compose.yml
install.sh          One-command install for macOS
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SERVER_HOST` | `localhost` | LAN IP embedded in M3U header's `x-tvg-url` |
