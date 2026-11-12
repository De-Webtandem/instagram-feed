#!/usr/bin/env python3
"""Instagram-feed voor dewebtandem.nl.

Haalt de nieuwste posts op via de officiele Instagram API, zet ze om naar kleine,
geluidloze bestanden en schrijft een complete statische map (feed.json + m/*) die in
zijn geheel naar Cloudflare Pages gaat. Alleen standaardbibliotheek + ffmpeg.

  sync.py sync --prev URL --out DIR [--force]   exit 0 = ok, 2 = harde fout (token/bug), 75 = tijdelijke fout
  sync.py refresh --token-out FILE              verlengt IG_TOKEN met 60 dagen, nieuw token naar FILE
  sync.py usage [--limit MIN]                   GitHub-billing-JSON op stdin, faalt boven de grens

Omgeving: IG_TOKEN (verplicht), IG_API_BASE, FFMPEG, INSTAGRAM_VIDEO=0 (alleen stilstaande beelden),
MAX_VIDEO_BYTES, GITHUB_OUTPUT (gezet door GitHub Actions).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = os.environ.get('IG_API_BASE', 'https://graph.instagram.com').rstrip('/')
FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')
AANTAL = 8            # tegels op de site
KANDIDATEN = 12       # posts die we bekijken, zodat een onbruikbare post overgeslagen kan worden
MAX_VIDEO = int(os.environ.get('MAX_VIDEO_BYTES', 4_000_000))
VIDEO_AAN = os.environ.get('INSTAGRAM_VIDEO', '1') != '0'
MAX_DOWNLOAD = 150_000_000
TIMEOUT = 30
FIELDS = 'id,media_type,media_url,thumbnail_url,permalink,children{id,media_type,media_url,thumbnail_url}'
BESTAND = re.compile(r'^m/\d+\.(jpg|mp4)$')
HIER = Path(__file__).resolve().parent


class Tijdelijk(Exception):
    """Instagram of Cloudflare even niet bruikbaar: niets publiceren, de live feed blijft staan."""


class Hard(Exception):
    """Token geweigerd of interne fout: direct melden."""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def output(**kv):
    pad = os.environ.get('GITHUB_OUTPUT')
    if pad:
        with open(pad, 'a', encoding='utf-8') as f:
            f.writelines(f'{k}={str(v).lower()}\n' for k, v in kv.items())


def haal(url, limiet=MAX_DOWNLOAD):
    deel = urllib.parse.urlsplit(url)
    # http alleen voor lokale tests (mock-API op 127.0.0.1)
    if not (deel.scheme == 'https' or (deel.scheme == 'http' and deel.hostname in ('127.0.0.1', 'localhost'))):
        raise ValueError(f'onverwachte URL ({deel.scheme}://{deel.hostname})')
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        data = r.read(limiet + 1)
    if len(data) > limiet:
        raise ValueError('bestand groter dan toegestaan')
    return data


def api_fout(e):
    try:
        return json.loads(e.read(100_000)).get('error') or {}
    except Exception:
        return {}


# ---------- Instagram ----------

def api_posts(token):
    url = f'{API}/me/media?' + urllib.parse.urlencode({'fields': FIELDS, 'limit': KANDIDATEN, 'access_token': token})
    try:
        body = haal(url, 5_000_000)
    except urllib.error.HTTPError as e:
        fout = api_fout(e)
        if fout.get('code') == 190:  # token verlopen, ingetrokken of ongeldig
            raise Hard(f'Instagram weigert het token: {fout.get("message", "")[:160]}. Maak een nieuw token aan (zie README).')
        raise Tijdelijk(f'Instagram API gaf HTTP {e.code} (code {fout.get("code")})')
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Tijdelijk(f'Instagram API niet bereikbaar: {type(e).__name__}')
    try:
        data = json.loads(body)['data']
        if not isinstance(data, list):
            raise TypeError
    except Exception:
        raise Tijdelijk('Instagram API gaf ongeldige JSON')
    return data


def plan(posts):
    """Per post: wat we tonen. Carrousel = eerste onderdeel; video zonder media_url (muziek met licentie) = stilstaand beeld."""
    uit = []
    for p in posts:
        if not isinstance(p, dict):
            continue
        bron = p
        if p.get('media_type') == 'CAROUSEL_ALBUM':
            kids = ((p.get('children') or {}).get('data')) or []
            bron = kids[0] if kids and isinstance(kids[0], dict) else None
        link = str(p.get('permalink') or '')
        pid, sid = str(p.get('id', '')), str((bron or {}).get('id', ''))
        if not bron or not pid.isdigit() or not sid.isdigit() or not link.startswith('https://www.instagram.com/'):
            log(f'post {pid[:20] or "?"} overgeslagen: onvolledige gegevens')
            continue
        video = bron.get('media_type') == 'VIDEO'
        soort = 'video' if video and VIDEO_AAN and bron.get('media_url') else ('still' if video else 'image')
        uit.append({'id': pid, 'sid': sid, 'plan': soort, 'link': link,
                    'video_url': bron.get('media_url') if soort == 'video' else None,
                    'beeld_url': bron.get('thumbnail_url') if video else bron.get('media_url')})
    return uit


def handtekening(k):
    return f"{k['id']}:{k['sid']}:{k['plan']}"


# ---------- media ----------

def ffmpeg(*args):
    r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', *args], capture_output=True, timeout=300)
    if r.returncode:
        raise RuntimeError('ffmpeg: ' + r.stderr.decode(errors='replace')[-300:])


def maak_beeld(url, doel, breedte):
    if not url:
        raise ValueError('geen beeld-URL')
    with tempfile.TemporaryDirectory() as t:
        bron = Path(t, 'in.jpg')  # Instagram-beelden zijn JPEG; extensie helpt minimale ffmpeg-builds
        bron.write_bytes(haal(url))
        ffmpeg('-i', str(bron), '-vf', f"scale='min({breedte},iw)':-2", '-frames:v', '1', '-q:v', '4', str(doel))


def maak_video(url, doel):
    with tempfile.TemporaryDirectory() as t:
        bron = Path(t, 'in.mp4')
        bron.write_bytes(haal(url))
        # geluid eruit (speelt toch gedempt af), max 30 s lus, 540 px breed: zelfde formaat als de huidige tegels
        ffmpeg('-i', str(bron), '-an', '-t', '30', '-vf', "scale='min(540,iw)':-2", '-c:v', 'libx264', '-preset', 'slow',
               '-crf', '28', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(doel))
    if doel.stat().st_size > MAX_VIDEO:
        raise ValueError(f'video {doel.stat().st_size // 1000} kB, limiet {MAX_VIDEO // 1000} kB')


def maak(k, uit):
    sid = k['sid']
    jpg, mp4 = uit / f'm/{sid}.jpg', uit / f'm/{sid}.mp4'
    basis = {'id': k['id'], 'sid': sid, 'plan': k['plan'], 'link': k['link']}
    try:
        if k['plan'] == 'video':
            try:
                maak_video(k['video_url'], mp4)
                maak_beeld(k['beeld_url'], jpg, 540)
                return {**basis, 'kind': 'video', 'src': f'm/{sid}.mp4', 'poster': f'm/{sid}.jpg'}
            except Exception as e:
                log(f'post {k["id"]}: video niet bruikbaar ({e}), valt terug op stilstaand beeld')
                mp4.unlink(missing_ok=True)
        maak_beeld(k['beeld_url'], jpg, 720)
        return {**basis, 'kind': 'image' if k['plan'] == 'image' else 'still', 'src': f'm/{sid}.jpg', 'poster': None}
    except Exception as e:
        log(f'post {k["id"]} overgeslagen: {e}')
        jpg.unlink(missing_ok=True)
        return None


def bestanden(p):
    return [f for f in (p.get('src'), p.get('poster')) if f]


def kopieer(prev, naam, uit):
    if not BESTAND.match(naam or ''):
        raise ValueError(f'ongeldige bestandsnaam in vorige feed: {str(naam)[:40]}')
    (uit / naam).write_bytes(haal(f'{prev}/{naam}'))


def hergebruik(o, k, prev, uit):
    try:
        for f in bestanden(o):
            kopieer(prev, f, uit)
        return {'id': k['id'], 'sid': k['sid'], 'plan': k['plan'], 'link': k['link'],
                'kind': o['kind'], 'src': o['src'], 'poster': o.get('poster')}
    except Exception as e:
        log(f'post {k["id"]}: vorige bestanden niet op te halen ({e}), opnieuw maken')
        for f in bestanden(o):
            if BESTAND.match(f or ''):
                (uit / f).unlink(missing_ok=True)
        return None


# ---------- feed ----------

def vorige_feed(prev):
    if not prev:
        return None
    try:
        f = json.loads(haal(f'{prev}/feed.json?t={int(time.time())}', 1_000_000))
        if not (isinstance(f, dict) and isinstance(f.get('posts'), list) and isinstance(f.get('plan'), list)):
            raise ValueError
        return f
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log('nog geen live feed gevonden, alles wordt nieuw gemaakt')
            return None
        raise Tijdelijk(f'live feed gaf HTTP {e.code}')
    except (urllib.error.URLError, OSError) as e:
        raise Tijdelijk(f'live feed niet bereikbaar: {type(e).__name__}')
    except ValueError:
        log('live feed.json is ongeldig, alles wordt nieuw gemaakt')
        return None


def bouw(kandidaten, vorige, prev, uit):
    (uit / 'm').mkdir(parents=True)
    oud = {(p.get('id'), p.get('sid'), p.get('plan')): p for p in (vorige or {}).get('posts', []) if isinstance(p, dict)}
    posts, bekeken = [], 0
    for k in kandidaten:
        if len(posts) == AANTAL:
            break
        bekeken += 1
        o = oud.get((k['id'], k['sid'], k['plan']))
        post = (o and hergebruik(o, k, prev, uit)) or maak(k, uit)
        if post:
            posts.append(post)
    if len(posts) < AANTAL:
        raise Tijdelijk(f'maar {len(posts)} van {AANTAL} posts bruikbaar, live feed blijft staan')
    # Vorige generatie een ronde bewaren: wie nog een gecachete feed.json heeft (max 5 min), krijgt geen 404.
    for o in oud.values():
        for f in bestanden(o):
            if BESTAND.match(f or '') and not (uit / f).exists():
                try:
                    kopieer(prev, f, uit)
                except Exception:
                    pass
    return posts, bekeken


def controleer(uit, feed):
    """Zelfcheck voor publicatie: elke verwezen file bestaat en is niet leeg."""
    if len(feed['posts']) != AANTAL:
        raise Hard('zelfcheck: verkeerd aantal posts')
    for p in feed['posts']:
        for f in bestanden(p):
            if not BESTAND.match(f) or not (uit / f).is_file() or (uit / f).stat().st_size == 0:
                raise Hard(f'zelfcheck: {f} ontbreekt of is leeg')
    json.loads((uit / 'feed.json').read_text(encoding='utf-8'))


def sync(a):
    token = os.environ.get('IG_TOKEN')
    if not token:
        raise Hard('IG_TOKEN ontbreekt')
    uit = Path(a.out)
    if uit.exists():  # nooit een oude uitvoermap per ongeluk publiceren
        shutil.rmtree(uit)
    prev = (a.prev or '').rstrip('/')
    vorige = vorige_feed(prev)
    kandidaten = plan(api_posts(token))
    if vorige and not a.force:
        n = len(vorige['plan'])
        if n and [handtekening(k) for k in kandidaten[:n]] == vorige['plan']:
            log('geen nieuwe of gewijzigde posts, niets te publiceren')
            output(ok=True, changed=False)
            return
    if a.check:  # goedkope controle: ffmpeg pas installeren als er echt iets te bouwen is
        log('nieuwe of gewijzigde posts gevonden')
        output(nodig=True)
        return
    tmp = Path(tempfile.mkdtemp(prefix='feed-', dir=uit.resolve().parent))
    try:
        posts, bekeken = bouw(kandidaten, vorige, prev, tmp)
        feed = {'versie': 1, 'gemaakt': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                'posts': posts, 'plan': [handtekening(k) for k in kandidaten[:bekeken]]}
        (tmp / 'feed.json').write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding='utf-8')
        shutil.copy(HIER / 'static' / '_headers', tmp / '_headers')
        controleer(tmp, feed)
        tmp.rename(uit)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log(f'feed klaar: {", ".join(p["kind"] for p in posts)}')
    output(ok=True, changed=True)


# ---------- onderhoud ----------

def refresh(a):
    token = os.environ.get('IG_TOKEN')
    if not token:
        raise Hard('IG_TOKEN ontbreekt')
    url = f'{API}/refresh_access_token?' + urllib.parse.urlencode({'grant_type': 'ig_refresh_token', 'access_token': token})
    try:
        d = json.loads(haal(url, 100_000))
    except urllib.error.HTTPError as e:
        raise Hard(f'verlengen geweigerd (HTTP {e.code}, code {api_fout(e).get("code")}); token mogelijk verlopen')
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Tijdelijk(f'verlengen mislukt: {type(e).__name__}')
    nieuw = d.get('access_token') if isinstance(d, dict) else None
    if not nieuw or not isinstance(nieuw, str):
        raise Hard('geen token in het antwoord van Instagram')
    if os.environ.get('GITHUB_ACTIONS'):
        print(f'::add-mask::{nieuw}', flush=True)
    Path(a.token_out).write_text(nieuw, encoding='utf-8')
    log(f'token verlengd, nog {int(d.get("expires_in", 0)) // 86400} dagen geldig')


def usage(a):
    d = json.load(sys.stdin)
    maand = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')
    minuten = sum(float(i.get('quantity') or 0) for i in d.get('usageItems', [])
                  if i.get('product') == 'actions' and i.get('unitType') == 'Minutes' and str(i.get('date', '')).startswith(maand))
    log(f'GitHub Actions {maand}: {minuten:.0f} minuten (waarschuwingsgrens {a.limit}, gratis 2000)')
    if minuten > a.limit:
        raise Hard(f'Actions-verbruik {minuten:.0f} min boven grens {a.limit}: zet de sync minder vaak (zie README)')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sync')
    s.add_argument('--prev', default='', help='basis-URL van de live feed (Cloudflare Pages)')
    s.add_argument('--out', required=True)
    s.add_argument('--force', action='store_true')
    s.add_argument('--check', action='store_true', help='alleen bepalen of er iets te publiceren is')
    r = sub.add_parser('refresh')
    r.add_argument('--token-out', required=True)
    u = sub.add_parser('usage')
    u.add_argument('--limit', type=int, default=1500)
    a = ap.parse_args(argv)
    try:
        {'sync': sync, 'refresh': refresh, 'usage': usage}[a.cmd](a)
    except Tijdelijk as e:
        log(f'TIJDELIJK: {e}')
        return 75
    except Hard as e:
        log(f'FOUT: {e}')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
