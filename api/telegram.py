"""Вебхук Telegram: отвечает на команды боту сразу. Вебхук включает сам notify.py при проходе по cron."""
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import notify  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, notify.WEBHOOK_SECRET):
            return self.reply(401)
        update = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        command = notify.parse_command(update)
        if command:
            name, args = command
            try:
                notify.answer_command(name, args)
            except Exception as e:
                # Отвечаем Telegram 200 в любом случае, иначе он будет повторять это сообщение.
                print(f"не удалось ответить на {name}: {type(e).__name__}")
                try:
                    notify.send_telegram("⚠️ Не получилось получить задачи из Plane, попробуйте позже.")
                except Exception:
                    pass
        self.reply(200)

    def reply(self, code):
        self.send_response(code)
        self.end_headers()
