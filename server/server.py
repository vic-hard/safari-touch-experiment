#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стенд Э0: раздача страниц из web/ и приём сессий POST /upload в data/.

Только стандартная библиотека: второй тулчейн в проекте не заводим (PLAN §5).
Запуск:

    python server/server.py
    cloudflared tunnel --url http://localhost:8000

Через туннель страница открывается по https — это обязательно (PLAN §6):
DeviceMotion без https не работает, а LAN-адрес упирается в брандмауэр Windows.
"""

import argparse
import datetime
import json
import os
import re
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")

MAX_BODY = 64 * 1024 * 1024  # сессия на 60 тапов — сотни килобайт; запас на серию с move
SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(value, default="na"):
    """Безопасный кусок имени файла из произвольного значения метаданных."""
    if value is None:
        return default
    text = SAFE.sub("-", str(value)).strip("-")
    return text[:40] or default


def session_filename(session):
    """Имя файла сессии: время старта + страница + участник + серия."""
    meta = session.get("meta") or {}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    parts = [
        stamp,
        slug(meta.get("page"), "page"),
        slug(meta.get("participant"), "p"),
        "s" + slug(meta.get("series"), "0"),
    ]
    mode = meta.get("mode")
    if mode:
        parts.append(slug(mode))
    return "-".join(parts) + ".json"


class Handler(SimpleHTTPRequestHandler):
    server_version = "SafariTouchExperiment/0.1"
    # HTTP/1.1 с keep-alive: туннель держит соединение открытым, и на HTTP/1.0
    # каждый запрос стоил бы нового коннекта.
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        kwargs["directory"] = WEB_DIR
        super().__init__(*args, **kwargs)

    # Кэш на телефоне — источник вечера отладки чужой версии страницы.
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/upload":
            self.send_error(404, "only POST /upload")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400, "bad Content-Length")
            return
        if length <= 0:
            self._json(400, {"ok": False, "error": "empty body"})
            return
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": "body too large"})
            return

        raw = self.rfile.read(length)
        try:
            session = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._json(400, {"ok": False, "error": "bad json: %s" % exc})
            return
        if not isinstance(session, dict):
            self._json(400, {"ok": False, "error": "session must be an object"})
            return

        os.makedirs(DATA_DIR, exist_ok=True)
        name = session_filename(session)
        path = os.path.join(DATA_DIR, name)
        # Не затираем уже снятую серию: повторить её в тех же условиях нельзя.
        base, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):
            path = "%s-%d%s" % (base, n, ext)
            n += 1
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(session, fh, ensure_ascii=False, indent=1)

        size = os.path.getsize(path)
        events = len(session.get("events") or [])
        frames = len(session.get("frames") or [])
        self.log_message("saved %s (%d B, events=%d, frames=%d)",
                         os.path.basename(path), size, events, frames)
        self._json(200, {"ok": True, "file": os.path.basename(path),
                         "bytes": size, "events": events, "frames": frames})

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._json(200, {"ok": True, "service": "safari-touch-experiment"})
            return
        super().do_GET()

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Стенд: статика web/ + POST /upload -> data/")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    if not os.path.isdir(WEB_DIR):
        print("нет каталога %s" % WEB_DIR, file=sys.stderr)
        return 2
    os.makedirs(DATA_DIR, exist_ok=True)

    # Однопоточный сервер обслуживает одно соединение за раз: туннель держит
    # соединения открытыми, и выгрузка серии встаёт в очередь за ними до
    # таймаута — на телефоне это выглядит как «POST не прошёл: Load failed».
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("статика:  %s" % WEB_DIR)
    print("сессии:   %s" % DATA_DIR)
    print("локально: http://localhost:%d/" % args.port)
    print("туннель:  cloudflared tunnel --url http://localhost:%d" % args.port)
    print("Ctrl+C — стоп")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
