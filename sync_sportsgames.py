#!/usr/bin/env python3
# Syncs live sportsgames.today events into channels.csv, EPG, and refreshes Jellyfin
import hashlib
import json, urllib.request, re, os, html
from datetime import datetime, timezone, timedelta

CSV = '/Users/tower/playlists/channels.csv'
JELLYFIN_URL = 'http://localhost:8096'
TASK_ID = "0c9ee3a88fc15547c6852205480da1fd"

def _openbao_token():
    try:
        with open(os.path.expanduser('~/server/SECRETS/openbao-init.txt')) as f:
            for line in f:
                if line.startswith('ROOT_TOKEN='):
                    return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return ''

def get_jellyfin_api_key():
    vt = _openbao_token()
    if not vt:
        return '274edad460e442d083e2bd6244de2ff3'  # fallback permanent API key
    try:
        req = urllib.request.Request(
            'http://10.0.10.98:8200/v1/secret/data/media/jellyfin',
            headers={'X-Vault-Token': vt}
        )
        d = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return d['data']['data'].get('api_key', '274edad460e442d083e2bd6244de2ff3')
    except Exception:
        return '274edad460e442d083e2bd6244de2ff3'

JELLYFIN_TOKEN = get_jellyfin_api_key()
GUIDE_TASK_ID = "bea9b218c97bbf98c5dc1303bdb9a0ca"
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

CATEGORIES = [
    'https://www.sportsgames.today/',
    'https://www.sportsgames.today/category/mlb/',
    'https://www.sportsgames.today/category/nfl/',
    'https://www.sportsgames.today/category/nba/',
    'https://www.sportsgames.today/category/nhl/',
    'https://www.sportsgames.today/category/soccer/',
    'https://www.sportsgames.today/category/mma-ufc/',
    'https://www.sportsgames.today/category/boxing/',
    'https://www.sportsgames.today/category/formula/',
    'https://www.sportsgames.today/category/motogp/',
    'https://www.sportsgames.today/category/wwe/',
    'https://www.sportsgames.today/category/ncaab/',
    'https://www.sportsgames.today/category/ncaaf/',
]


def detect_sport(href, title, embed_url=''):
    # Extract sport from embedsports.me URL path - most reliable
    m = re.search(r'embedsports\.me/([^/]+)/', embed_url or '')
    if m:
        path = m.group(1).lower()
        path_map = {
            'boxing': ('Sports - Boxing', 'boxing'), 'mma': ('Sports - MMA', 'mma'),
            'nfl': ('Sports - NFL', 'nfl'), 'nba': ('Sports - NBA', 'nba'),
            'mlb': ('Sports - MLB', 'mlb'), 'nhl': ('Sports - NHL', 'nhl'),
            'cfl': ('Sports - CFL', 'cfl'), 'wnba': ('Sports - WNBA', 'wnba'),
            'soccer': ('Sports - Soccer', 'soccer'), 'tennis': ('Sports - Tennis', 'tennis'),
            'golf': ('Sports - Golf', 'golf'), 'rugby': ('Sports - Rugby', 'rugby'),
            'cricket': ('Sports - Cricket', 'cricket'), 'motogp': ('Sports - MotoGP', 'motogp'),
            'formula': ('Sports - F1', 'f1'), 'wwe': ('Sports - WWE', 'wwe'),
            'ncaa': ('Sports - NCAA', 'ncaa'), 'nascar': ('Sports - NASCAR', 'live'),
        }
        if path in path_map:
            return path_map[path]
    text = (href + ' ' + title).lower()
    if any(x in text for x in ['ufc', ' mma ', 'cage warriors', 'bellator', 'one championship', 'pfl']):
        return 'Sports - MMA', 'mma'
    if any(x in text for x in ['boxing', 'wbc', 'wba', 'wbo', 'heavyweight', 'featherweight']):
        return 'Sports - Boxing', 'boxing'
    if any(x in text for x in ['nfl', ' football', 'super bowl']):
        return 'Sports - NFL', 'nfl'
    if any(x in text for x in ['nba', 'basketball']) or ('warriors' in text and 'cage' not in text and 'golden state' in text):
        return 'Sports - NBA', 'nba'
    if any(x in text for x in ['mlb', 'baseball', 'yankees', 'dodgers', 'red sox', 'mariners',
                                'twins', 'diamondbacks', 'orioles', 'pirates', 'rockies',
                                'angels', 'athletics', 'astros', 'cubs', 'mets', 'braves',
                                'phillies', 'cardinals', 'padres', 'giants', 'rangers',
                                'blue jays', 'rays', 'tigers', 'guardians', 'white sox',
                                'reds', 'brewers', 'royals', 'nationals', 'world series']):
        return 'Sports - MLB', 'mlb'
    if any(x in text for x in ['nhl', 'hockey', 'stanley cup']):
        return 'Sports - NHL', 'nhl'
    if any(x in text for x in ['wnba', 'fever', 'storm', 'mercury', 'dream', 'lynx', 'sparks', 'mystics', 'aces', 'liberty', 'wings']):
        return 'Sports - WNBA', 'wnba'
    if any(x in text for x in ['cfl', 'roughriders', 'stampeders', 'alouettes', 'argonauts', 'elks', 'redblacks', 'blue bombers']):
        return 'Sports - CFL', 'cfl'
    if any(x in text for x in ['formula', 'grand prix', ' f1 ']):
        return 'Sports - F1', 'f1'
    if 'motogp' in text:
        return 'Sports - MotoGP', 'motogp'
    if any(x in text for x in ['wwe', 'wrestling', 'smackdown', 'wrestlemania']):
        return 'Sports - WWE', 'wwe'
    if any(x in text for x in ['ncaa', 'college football', 'college basketball']):
        return 'Sports - NCAA', 'ncaa'
    if any(x in text for x in ['soccer', 'premier league', 'champions league', 'la liga', 'bundesliga', 'serie a', 'mls', 'copa', 'euro ', 'world cup', 'nations league']):
        return 'Sports - Soccer', 'soccer'
    if 'tennis' in text or 'wimbledon' in text:
        return 'Sports - Tennis', 'tennis'
    if 'golf' in text or 'pga' in text:
        return 'Sports - Golf', 'golf'
    if 'rugby' in text:
        return 'Sports - Rugby', 'rugby'
    if 'cricket' in text:
        return 'Sports - Cricket', 'cricket'
    if ' – ' in title or (' vs ' in title.lower() and not any(x in text for x in ['nfl','nba','mlb','nhl','mma','boxing','ufc','cfl','wnba'])):
        return 'Sports - Soccer', 'soccer'
    return 'Sports - Live', 'live'
def fetch(url, referer=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    if referer:
        req.add_header('Referer', referer)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f'  fetch error {url}: {e}')
        return ''

TEAM_ABBREVS = {
    'mlb': {
        'yankees': 'nyy', 'red sox': 'bos', 'mariners': 'sea', 'twins': 'min',
        'diamondbacks': 'ari', 'orioles': 'bal', 'dodgers': 'lad', 'angels': 'laa',
        'athletics': 'oak', 'astros': 'hou', 'pirates': 'pit', 'rockies': 'col',
        'guardians': 'cle', 'mets': 'nym', 'phillies': 'phi', 'nationals': 'was',
        'rays': 'tb', 'giants': 'sf', 'marlins': 'mia', 'brewers': 'mil',
        'braves': 'atl', 'cubs': 'chc', 'cardinals': 'stl', 'padres': 'sd',
        'rangers': 'tex', 'blue jays': 'tor', 'tigers': 'det',
        'white sox': 'chw', 'reds': 'cin', 'royals': 'kc',
    },
    'nba': {
        'lakers': 'lal', 'celtics': 'bos', 'warriors': 'gs', 'bulls': 'chi',
        'heat': 'mia', 'nets': 'bkn', 'knicks': 'ny', 'sixers': 'phi',
        'suns': 'phx', 'nuggets': 'den', 'bucks': 'mil', 'hawks': 'atl',
        'jazz': 'utah', 'clippers': 'lac', 'mavericks': 'dal', 'rockets': 'hou',
        'spurs': 'sa', 'raptors': 'tor', 'thunder': 'okc', 'timberwolves': 'min',
        'pacers': 'ind', 'cavaliers': 'cle', 'pistons': 'det', 'magic': 'orl',
        'wizards': 'wsh', 'pelicans': 'no', 'trail blazers': 'por', 'kings': 'sac',
        'grizzlies': 'mem', 'hornets': 'cha',
    },
    'nfl': {
        'patriots': 'ne', 'cowboys': 'dal', 'packers': 'gb', 'chiefs': 'kc',
        '49ers': 'sf', 'ravens': 'bal', 'bills': 'buf', 'bengals': 'cin',
        'eagles': 'phi', 'rams': 'lar', 'buccaneers': 'tb', 'steelers': 'pit',
        'broncos': 'den', 'chargers': 'lac', 'bears': 'chi', 'colts': 'ind',
        'vikings': 'min', 'saints': 'no', 'seahawks': 'sea', 'falcons': 'atl',
        'giants': 'nyg', 'jets': 'nyj', 'lions': 'det', 'browns': 'cle',
        'commanders': 'wsh', 'texans': 'hou', 'jaguars': 'jax', 'titans': 'ten',
        'raiders': 'lv', 'cardinals': 'ari', 'dolphins': 'mia', 'panthers': 'car',
    },
}
_ESPN_CDN = "https://a.espncdn.com/combiner/i?img=/i/teamlogos"

def get_logo_url(title, sport_key):
    """Try to get a team-specific ESPN logo; fall back to sport badge."""
    tl = title.lower()
    league_map = {'mlb': 'mlb', 'nba': 'nba', 'nfl': 'nfl'}
    espn_league = league_map.get(sport_key)
    if espn_league and espn_league in TEAM_ABBREVS:
        teams = TEAM_ABBREVS[espn_league]
        found = []
        for name, abbrev in teams.items():
            if name in tl:
                found.append((tl.index(name), abbrev))
        if found:
            found.sort(key=lambda x: -x[0])  # pick last-mentioned = home team
            abbrev = found[0][1]
            return f"{_ESPN_CDN}/{espn_league}/500/{abbrev}.png"
    return f"http://localhost:8765/icon/{sport_key}"

def fetch_events():
    seen = {}
    for cat in CATEGORIES:
        page = fetch(cat)
        links = re.findall(r'href="(https://www\.sportsgames\.today/[^"]+)"[^>]*>\s*([^<]{5,})', page)
        for href, title in links:
            href = href.rstrip('/')+ '/'
            if re.search(r'/category/|sportsgames\.today/$', href):
                continue
            if href not in seen and len(title.strip()) > 5:
                seen[href] = html.unescape(title.strip())

    events = []
    for href, title in seen.items():
        # Extract embed URL from article page
        page = fetch(href)
        m = re.search(r'streams\.center/embed/([^"\'<\s]+)', page)
        embed_path = m.group(1) if m else None
        embed_url = f'https://streams.center/embed/{embed_path}' if embed_path else None
        if not embed_url:
            m2 = re.search(r'iframe[^>]+src="(https://embedsports[.]me/[^"]+)"', page)
            if m2: embed_url = m2.group(1)
        if not embed_url:
            print(f'  SKIP (no embed): {title}')
            continue
        pid = int(hashlib.md5(href.encode()).hexdigest(), 16) % 100000
        group, sport_key = detect_sport(href, title, embed_url or '')
        icon_url = get_logo_url(title, sport_key)
        events.append({
            'id': pid,
            'title': title,
            'href': href,
            'embedUrl': embed_url,
            'group': group,
            'iconUrl': icon_url,
        })
        status = embed_url.split('/')[-1] if embed_url else 'no embed'
        print(f'  {title} -> {status}')
    return events

def xmltv_ts(dt):
    return dt.strftime('%Y%m%d%H%M%S') + ' +0000'

def main():
    with open(CSV) as f:
        content = f.read()
    lines = content.split('\n')
    header = lines[0]
    rows = [l for l in lines[1:] if l and 'sportsgames' not in l.lower()]

    print('Scraping sportsgames.today...')
    events = fetch_events()
    for p in events:
        title = p['title'].replace(',', '')
        pid = p['id']
        embed_url = p.get('embedUrl') or ''
        proxy_target = embed_url if embed_url else p['href']
        proxy_url = f'http://localhost:8766/proxy?url={proxy_target}'
        group = p.get('group', 'Sports')
        icon_url = p.get('iconUrl', '')
        rows.append(f'{title},{group},,,,sportsgames-{pid},{icon_url},{proxy_url},sportsgames')

    with open(CSV, 'w') as f:
        f.write('\n'.join([header] + rows) + '\n')
    print(f'Done: {len(events)} events, {len(rows)} total channels')

    # Delete + re-add the playlist_server tuner to bust Jellyfin's M3U cache,
    # then trigger channel scan. The scheduled task alone caches and doesn't re-fetch.
    try:
        list_req = urllib.request.Request(
            f'{JELLYFIN_URL}/LiveTv/TunerHosts',
            headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_TOKEN}"'}
        )
        hosts = json.loads(urllib.request.urlopen(list_req, timeout=5).read())
        tuner_id = next((h['Id'] for h in hosts if '8765' in h.get('Url', '')), None)
        if tuner_id:
            del_req = urllib.request.Request(
                f'{JELLYFIN_URL}/LiveTv/TunerHosts?id={tuner_id}',
                method='DELETE',
                headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_TOKEN}"'}
            )
            urllib.request.urlopen(del_req, timeout=5)
            add_body = json.dumps({
                'Url': 'http://10.0.10.98:8765/playlist.m3u',
                'Type': 'M3U',
                'FriendlyName': 'IPTV-org Sports (validated direct HLS)',
                'AllowHWTranscoding': True,
                'TunerCount': 4,
                'IgnoreDts': True,
            }).encode()
            add_req = urllib.request.Request(
                f'{JELLYFIN_URL}/LiveTv/TunerHosts',
                data=add_body,
                method='POST',
                headers={
                    'Authorization': f'MediaBrowser Token="{JELLYFIN_TOKEN}"',
                    'Content-Type': 'application/json',
                }
            )
            urllib.request.urlopen(add_req, timeout=5)
        req = urllib.request.Request(
            f'{JELLYFIN_URL}/ScheduledTasks/Running/{TASK_ID}',
            method='POST',
            headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_TOKEN}"'}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception as e2:
            if '405' not in str(e2):  # 405 = already running, that's fine
                raise
        print('Jellyfin tuner re-added and channel scan triggered')
    except Exception as e:
        print(f'Jellyfin refresh error: {e}')
    try:
        guide_req = urllib.request.Request(
            f'{JELLYFIN_URL}/ScheduledTasks/Running/bea9b218c97bbf98c5dc1303bdb9a0ca',
            method='POST',
            headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_TOKEN}"'}
        )
        urllib.request.urlopen(guide_req, timeout=5)
        print('Jellyfin guide refreshed')
    except Exception as e:
        print(f'Guide refresh error: {e}')

    # DYNAMIC: no prewarming - channels start on-demand when watched,
    # proxy reaps them 90s after the last viewer leaves.
    print(str(len(events)) + ' sports events available (on-demand)')
main()