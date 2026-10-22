"""Vercel Cron: проход по задачам Plane (расписание — в vercel.json)."""
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notify  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Vercel Cron присылает Authorization: Bearer $CRON_SECRET — без него адрес мог бы дёргать кто угодно.
        secret = os.environ.get("CRON_SECRET", "")
        if not secret or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {secret}"):
            return self.reply(401, "unauthorized")
        failed = notify.main()
        self.reply(500 if failed else 200, f"failed sends: {failed}" if failed else "ok")

    def reply(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())
