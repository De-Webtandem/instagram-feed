"""Lokale tests met een nep-Instagram-API en een nep-Cloudflare-site. Geen netwerk, geen token.

  FFMPEG=/pad/naar/ffmpeg python -m unittest -v test_sync
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import sync

FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')
FFPROBE = str(Path(FFMPEG).with_name(Path(FFMPEG).name.replace('ffmpeg', 'ffprobe')))
STATE = {}


class Nep(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def stuur(self, code, body=b'', ctype='application/octet-stream'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        STATE['hits'].append(u.path)
        if u.path == '/me/media':
            q = parse_qs(u.query)
            assert q['access_token'] == ['geheim'], 'token niet meegestuurd'
            soort, waarde = STATE['api']
            if soort == 'json':
                return self.stuur(200, json.dumps({'data': waarde}).encode(), 'application/json')
            if soort == 'raw':
                return self.stuur(200, waarde, 'application/json')
            return self.stuur(waarde[0], json.dumps(waarde[1]).encode(), 'application/json')
        if u.path == '/refresh_access_token':
            return self.stuur(*STATE['refresh'])
        if u.path.startswith('/media/'):
            data = STATE['ig'].get(u.path[7:])
            return self.stuur(200, data) if data is not None else self.stuur(404)
        if u.path.startswith('/site/'):
            f = STATE['site'] / u.path[6:]
            return self.stuur(200, f.read_bytes()) if f.is_file() else self.stuur(404)
        self.stuur(404)


class Basis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        # fixtures: echte Instagram-foto en een 3 s reel (720x1280) met geluidsspoor, om het strippen te testen
        cls.foto = Path(__file__).parent / 'fixtures' / 'foto.jpg'
        cls.video = Path(__file__).parent / 'fixtures' / 'video.mp4'
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), Nep)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.srv.server_port}'
        sync.API, sync.FFMPEG = cls.base, FFMPEG

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.werk = Path(tempfile.mkdtemp(dir=self.tmp))
        STATE.clear()
        STATE.update(hits=[], ig={}, site=self.werk / 'site', api=('json', []))
        STATE['site'].mkdir()
        self.posts = []
        self.nr = 0
        os.environ['IG_TOKEN'] = 'geheim'
        sync.VIDEO_AAN, sync.MAX_VIDEO = True, 4_000_000

    # --- helpers ---
    def nieuw(self, soort='image', muziek=False, kind_soort='IMAGE'):
        """Nieuwe post bovenaan de feed, zoals Instagram ze teruggeeft."""
        self.nr += 1
        i = 1000 + self.nr
        def media(j, t):
            d = {'id': str(j), 'media_type': t}
            if t == 'VIDEO':
                if not muziek:
                    d['media_url'] = f'{self.base}/media/v{j}.mp4'
                    STATE['ig'][f'v{j}.mp4'] = self.video.read_bytes()
                d['thumbnail_url'] = f'{self.base}/media/t{j}.jpg'
                STATE['ig'][f't{j}.jpg'] = self.foto.read_bytes()
            else:
                d['media_url'] = f'{self.base}/media/f{j}.jpg'
                STATE['ig'][f'f{j}.jpg'] = self.foto.read_bytes()
            return d
        if soort == 'carousel':
            p = {'id': str(i), 'media_type': 'CAROUSEL_ALBUM', 'children': {'data': [media(i * 10, kind_soort), media(i * 10 + 1, 'IMAGE')]}}
        else:
            p = media(i, 'VIDEO' if soort == 'video' else 'IMAGE')
        p['permalink'] = f'https://www.instagram.com/p/T{i}/'
        self.posts.insert(0, p)
        STATE['api'] = ('json', self.posts)
        return p

    def start(self, n=8):
        soorten = ['video', 'image', 'video', 'carousel', 'video', 'image', 'video', 'image']
        for i in range(n):
            self.nieuw(soorten[i % 8])

    def onbereikbaar(self, post):
        """Alle media van een post geven 404 bij Instagram."""
        delen = [post] + post.get('children', {}).get('data', [])
        for d in delen:
            for k in ('media_url', 'thumbnail_url'):
                if k in d:
                    STATE['ig'].pop(d[k].rsplit('/', 1)[1], None)

    def draai(self, *extra):
        out = self.werk / 'out'
        gh = self.werk / 'gh_output'
        gh.write_text('')
        os.environ['GITHUB_OUTPUT'] = str(gh)
        rc = sync.main(['sync', '--prev', f'{self.base}/site', '--out', str(out), *extra])
        uitvoer = dict(r.split('=') for r in gh.read_text().split())
        return rc, uitvoer, out

    def publiceer(self, out):
        """Zoals Cloudflare Pages: een deploy bevat alleen wat je uploadt."""
        shutil.rmtree(STATE['site'])
        shutil.copytree(out, STATE['site'])

    def feed(self):
        return json.loads((STATE['site'] / 'feed.json').read_text(encoding='utf-8'))

    def assert_compleet(self):
        f = self.feed()
        self.assertEqual(len(f['posts']), 8)
        for p in f['posts']:
            for naam in filter(None, (p['src'], p['poster'])):
                self.assertTrue((STATE['site'] / naam).stat().st_size > 0, naam)
        return f

    def ids(self):
        return [p['id'] for p in self.feed()['posts']]


class Sync(Basis):
    def test_eerste_sync_alle_soorten(self):
        self.start()
        rc, o, out = self.draai()
        self.assertEqual((rc, o['changed']), (0, 'true'))
        self.publiceer(out)
        f = self.assert_compleet()
        self.assertEqual(self.ids(), [p['id'] for p in self.posts[:8]])
        self.assertEqual([p['kind'] for p in f['posts']], ['image', 'video', 'image', 'video', 'image', 'video', 'image', 'video'])
        self.assertTrue((STATE['site'] / '_headers').is_file())
        # video: geen geluid, 540 breed, poster aanwezig
        mp4 = STATE['site'] / next(p['src'] for p in f['posts'] if p['kind'] == 'video')
        streams = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,width', '-of', 'json', str(mp4)],
                                 capture_output=True, text=True, check=True).stdout
        s = json.loads(streams)['streams']
        self.assertEqual([x['codec_type'] for x in s], ['video'])
        self.assertEqual(s[0]['width'], 540)

    def test_check_modus_bouwt_niets(self):
        self.start()
        rc, o, out = self.draai('--check')
        self.assertEqual((rc, o), (0, {'nodig': 'true'}))
        self.assertFalse(out.exists())
        self.assertFalse([h for h in STATE['hits'] if h.startswith('/media/')])
        self.publiceer(self.draai()[2])
        self.assertEqual(self.draai('--check')[1], {'ok': 'true', 'changed': 'false'})

    def test_geen_wijziging_geen_deploy(self):
        self.start()
        self.publiceer(self.draai()[2])
        STATE['hits'].clear()
        rc, o, out = self.draai()
        self.assertEqual((rc, o['changed']), (0, 'false'))
        self.assertFalse(out.exists())
        self.assertFalse([h for h in STATE['hits'] if h.startswith('/media/')], 'geen media downloaden zonder wijziging')

    def test_opeenvolgende_syncs_behouden_media(self):
        self.start()
        self.publiceer(self.draai()[2])
        for ronde, soort in enumerate(['video', 'image', 'carousel', 'video'], 1):
            vorige = self.feed()
            STATE['hits'].clear()
            self.nieuw(soort)
            rc, o, out = self.draai()
            self.assertEqual((rc, o['changed']), (0, 'true'), f'ronde {ronde}')
            self.publiceer(out)
            f = self.assert_compleet()
            self.assertEqual(self.ids(), [p['id'] for p in self.posts[:8]])
            # alleen de nieuwe post wordt bij Instagram gedownload, de rest komt uit de vorige deploy
            ig = {h for h in STATE['hits'] if h.startswith('/media/')}
            nieuw = self.posts[0]
            self.assertTrue(ig and all(str(nieuw['id']) in h for h in ig), ig)
            # bestanden van de vorige generatie staan er nog (gecachete feed.json bezoekers)
            for p in vorige['posts']:
                for naam in filter(None, (p['src'], p['poster'])):
                    self.assertTrue((STATE['site'] / naam).is_file(), f'{naam} verdwenen in ronde {ronde}')
            # maar niet alles van twee generaties terug
            in_gebruik = {n for p in f['posts'] + vorige['posts'] for n in (p['src'], p['poster']) if n}
            self.assertEqual({f'm/{x.name}' for x in (STATE['site'] / 'm').iterdir()}, in_gebruik)

    def test_muziek_met_licentie_wordt_stilstaand_beeld_en_blijft_stabiel(self):
        self.start(7)
        self.nieuw('video', muziek=True)
        rc, o, out = self.draai()
        self.publiceer(out)
        p = self.feed()['posts'][0]
        self.assertEqual((p['kind'], p['poster'], p['plan']), ('still', None, 'still'))
        self.assertEqual(self.draai()[1]['changed'], 'false')

    def test_te_grote_video_valt_terug_en_herhaalt_niet(self):
        self.start()
        sync.MAX_VIDEO = 1000
        rc, o, out = self.draai()
        self.publiceer(out)
        f = self.assert_compleet()
        self.assertEqual({p['kind'] for p in f['posts']}, {'image', 'still'})
        self.assertFalse(list((STATE['site'] / 'm').glob('*.mp4')))
        self.assertEqual(self.draai()[1]['changed'], 'false', 'geen deploy per uur door fallback')

    def test_video_uitzetten(self):
        self.start()
        self.publiceer(self.draai()[2])
        sync.VIDEO_AAN = False
        rc, o, out = self.draai()
        self.assertEqual(o['changed'], 'true')
        self.publiceer(out)
        self.assertNotIn('video', {p['kind'] for p in self.assert_compleet()['posts']})

    def test_carrousel_met_video_als_eerste(self):
        self.start(7)
        self.nieuw('carousel', kind_soort='VIDEO')
        self.publiceer(self.draai()[2])
        p = self.feed()['posts'][0]
        self.assertEqual((p['kind'], p['sid']), ('video', str(int(p['id']) * 10)))

    def test_ontbrekende_media_slaat_post_over(self):
        self.start(9)
        self.onbereikbaar(self.posts[0])  # nieuwste post onbereikbaar
        rc, o, out = self.draai()
        self.assertEqual(rc, 0)
        self.publiceer(out)
        self.assertEqual(self.ids(), [p['id'] for p in self.posts[1:9]])

    def test_te_weinig_bruikbare_posts_houdt_live_feed(self):
        self.start()
        self.publiceer(self.draai()[2])
        voor = self.feed()
        self.nieuw('image')
        STATE['ig'].clear()  # alle Instagram-media onbereikbaar
        shutil.rmtree(STATE['site'] / 'm')  # en de vorige bestanden ook
        rc, o, out = self.draai()
        self.assertEqual(rc, 75)
        self.assertNotIn('changed', o)
        self.assertFalse(out.exists())
        self.assertEqual(self.feed(), voor)

    def test_ontbrekend_bestand_op_live_site_wordt_hersteld(self):
        self.start()
        self.publiceer(self.draai()[2])
        weg = self.feed()['posts'][3]['src']
        (STATE['site'] / weg).unlink()
        self.nieuw('image')
        rc, o, out = self.draai()
        self.publiceer(out)
        self.assert_compleet()

    def test_verlopen_token(self):
        self.start()
        self.publiceer(self.draai()[2])
        voor = self.feed()
        STATE['api'] = ('status', (400, {'error': {'message': 'Error validating access token: Session has expired', 'type': 'OAuthException', 'code': 190}}))
        rc, o, out = self.draai()
        self.assertEqual((rc, o), (2, {}))
        self.assertEqual(self.feed(), voor)

    def test_rate_limit_is_tijdelijk(self):
        STATE['api'] = ('status', (400, {'error': {'message': 'Application request limit reached', 'type': 'OAuthException', 'code': 4}}))
        self.assertEqual(self.draai()[0], 75)

    def test_api_storing_en_corrupte_api_json(self):
        STATE['api'] = ('status', (500, {}))
        self.assertEqual(self.draai()[0], 75)
        STATE['api'] = ('raw', b'{"data": [')
        self.assertEqual(self.draai()[0], 75)
        STATE['api'] = ('raw', b'{"data": {"x": 1}}')
        self.assertEqual(self.draai()[0], 75)

    def test_corrupte_live_feed_wordt_volledig_herbouwd(self):
        self.start()
        self.publiceer(self.draai()[2])
        (STATE['site'] / 'feed.json').write_text('{"posts": [kapot')
        rc, o, out = self.draai()
        self.assertEqual((rc, o['changed']), (0, 'true'))
        self.publiceer(out)
        self.assert_compleet()

    def test_vreemde_bestandsnaam_in_live_feed_wordt_niet_gevolgd(self):
        self.start()
        self.publiceer(self.draai()[2])
        f = self.feed()
        f['posts'][0]['src'] = '../../geheim.txt'
        (STATE['site'] / 'feed.json').write_text(json.dumps(f))
        self.nieuw('image')
        rc, o, out = self.draai()
        self.assertEqual(rc, 0)
        self.publiceer(out)
        self.assert_compleet()
        self.assertFalse(any('geheim' in h for h in STATE['hits']))

    def test_gewijzigde_post_wordt_opnieuw_verwerkt(self):
        self.start()
        self.publiceer(self.draai()[2])
        car = next(p for p in self.posts if p['media_type'] == 'CAROUSEL_ALBUM')
        car['children']['data'].pop(0)  # eerste foto uit de carrousel verwijderd
        rc, o, out = self.draai()
        self.assertEqual(o['changed'], 'true')
        self.publiceer(out)
        p = next(p for p in self.feed()['posts'] if p['id'] == car['id'])
        self.assertEqual(p['sid'], car['children']['data'][0]['id'])
        self.assert_compleet()

    def test_verwijderde_post(self):
        self.start(9)
        self.publiceer(self.draai()[2])
        self.posts.pop(2)
        rc, o, out = self.draai()
        self.assertEqual(o['changed'], 'true')
        self.publiceer(out)
        self.assertEqual(self.ids(), [p['id'] for p in self.posts[:8]])

    def test_herhaalde_geforceerde_deploys_zijn_gelijk(self):
        self.start()
        self.publiceer(self.draai()[2])
        eerst = self.feed()
        for _ in range(2):
            rc, o, out = self.draai('--force')
            self.assertEqual(o['changed'], 'true')
            self.publiceer(out)
        tweede = self.feed()
        self.assertEqual(eerst['posts'], tweede['posts'])
        self.assertEqual(eerst['plan'], tweede['plan'])

    def test_onveilige_url_van_api_wordt_geweigerd(self):
        self.start(10)
        self.posts[0]['media_url'] = 'file:///etc/passwd'
        self.posts[1]['permalink'] = 'https://evil.example/p/1'
        self.posts[2]['id'] = '../../x'
        STATE['api'] = ('json', self.posts)
        rc, o, out = self.draai()
        self.assertEqual(rc, 75)  # maar 7 bruikbaar: live feed blijft
        self.nieuw('image')
        rc, o, out = self.draai()
        self.assertEqual(rc, 0)
        self.publiceer(out)
        self.assertEqual(self.ids(), [self.posts[0]['id']] + [p['id'] for p in self.posts[4:11]])

    def test_token_staat_niet_in_de_feed(self):
        self.start()
        self.publiceer(self.draai()[2])
        for f in STATE['site'].rglob('*'):
            if f.is_file():
                self.assertNotIn(b'geheim', f.read_bytes(), f.name)


class Onderhoud(Basis):
    def test_token_verlengen(self):
        STATE['refresh'] = (200, json.dumps({'access_token': 'nieuw-token', 'token_type': 'bearer', 'expires_in': 5184000}).encode())
        doel = self.werk / 'tok'
        self.assertEqual(sync.main(['refresh', '--token-out', str(doel)]), 0)
        self.assertEqual(doel.read_text(), 'nieuw-token')

    def test_token_verlengen_geweigerd(self):
        STATE['refresh'] = (400, json.dumps({'error': {'code': 190}}).encode())
        self.assertEqual(sync.main(['refresh', '--token-out', str(self.werk / 'tok')]), 2)

    def test_verbruik(self):
        import io, sys, datetime
        maand = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m')
        d = {'usageItems': [
            {'date': f'{maand}-01T00:00:00Z', 'product': 'actions', 'unitType': 'Minutes', 'quantity': 900},
            {'date': f'{maand}-01T00:00:00Z', 'product': 'actions', 'unitType': 'Minutes', 'quantity': 300},
            {'date': f'{maand}-01T00:00:00Z', 'product': 'actions', 'unitType': 'GigabyteHours', 'quantity': 5000},
            {'date': '2020-01-01T00:00:00Z', 'product': 'actions', 'unitType': 'Minutes', 'quantity': 9999}]}
        oud = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(d))
            self.assertEqual(sync.main(['usage', '--limit', '1500']), 0)
            sys.stdin = io.StringIO(json.dumps(d))
            self.assertEqual(sync.main(['usage', '--limit', '1000']), 2)
        finally:
            sys.stdin = oud


if __name__ == '__main__':
    unittest.main()
