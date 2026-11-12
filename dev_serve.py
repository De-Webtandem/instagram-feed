"""Alleen lokaal testen: serveert een map met CORS, zoals Cloudflare Pages met static/_headers.

  python dev_serve.py MAP [POORT]
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Cors(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()


if __name__ == '__main__':
    poort = int(sys.argv[2]) if len(sys.argv) > 2 else 8090
    ThreadingHTTPServer(('127.0.0.1', poort), partial(Cors, directory=sys.argv[1])).serve_forever()
