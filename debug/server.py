#!/usr/bin/env python3
"""
debug/server.py — универсальный сервер для отладки.

Запуск: python debug/server.py [PORT] [ROOT_DIR]
По умолчанию: PORT=8765, ROOT_DIR=.

Что делает:
  - Раздаёт статические файлы из ROOT_DIR
  - Принимает POST /api/log — логи из браузера
  - Принимает POST /api/cli  — логи из терминала
  - GET /api/logs            — все собранные логи (для Claude)
  - GET /debug               — мини-интерфейс просмотра логов
"""

import sys
import json
import time
import datetime
import pathlib
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT     = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
ROOT_DIR = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else pathlib.Path(".").resolve()
LOG_FILE = pathlib.Path(__file__).parent / "logs" / "unified.jsonl"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def write_log(entry: dict):
    entry.setdefault("ts", datetime.datetime.utcnow().isoformat() + "Z")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    level = entry.get("level", "info").upper()
    src   = entry.get("source", "?")
    msg   = entry.get("message", "")[:120]
    print(f"[{level}] [{src}] {msg}")


DEBUG_UI = """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Debug Logs</title>
<style>
  body{background:#0c0c0c;color:#e8e4d9;font-family:monospace;padding:20px}
  h1{color:#fbbf24;margin-bottom:16px}
  #log{display:flex;flex-direction:column;gap:6px}
  .entry{padding:8px 12px;border-radius:6px;font-size:13px;background:#141414;border-left:3px solid #444}
  .entry.error{border-color:#f87171;background:#1a0e0e}
  .entry.warn{border-color:#fbbf24;background:#1a160a}
  .entry.info{border-color:#22d3ee}
  .ts{color:#5a564e;font-size:11px;margin-right:8px}
  .src{color:#4ade80;margin-right:8px}
  .level{font-weight:bold;margin-right:8px}
  #controls{margin-bottom:16px;display:flex;gap:10px}
  button{background:#222;color:#e8e4d9;border:1px solid #444;padding:6px 14px;border-radius:6px;cursor:pointer}
  button:hover{background:#333}
  #filter{background:#141414;color:#e8e4d9;border:1px solid #444;padding:6px 12px;border-radius:6px;width:250px}
</style>
</head>
<body>
<h1>🐛 Debug Logs</h1>
<div id="controls">
  <button onclick="loadLogs()">↻ Обновить</button>
  <button onclick="clearView()">✕ Очистить вид</button>
  <input id="filter" placeholder="Фильтр по тексту..." oninput="applyFilter()">
</div>
<div id="log"></div>
<script>
let allLogs = [];
async function loadLogs(){
  const r = await fetch('/api/logs');
  allLogs = await r.json();
  renderLogs(allLogs);
}
function renderLogs(logs){
  const el = document.getElementById('log');
  el.innerHTML = '';
  [...logs].reverse().forEach(e => {
    const d = document.createElement('div');
    const lvl = (e.level||'info').toLowerCase();
    d.className = 'entry ' + lvl;
    d.innerHTML = `<span class="ts">${(e.ts||'').slice(11,19)}</span>` +
      `<span class="src">[${e.source||'?'}]</span>` +
      `<span class="level">${lvl.toUpperCase()}</span>` +
      `<span>${escHtml(e.message||'')}</span>` +
      (e.stack ? `<pre style="margin-top:4px;color:#9e9a8e;font-size:11px">${escHtml(e.stack)}</pre>` : '');
    el.appendChild(d);
  });
}
function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function applyFilter(){
  const q = document.getElementById('filter').value.toLowerCase();
  renderLogs(allLogs.filter(e => JSON.stringify(e).toLowerCase().includes(q)));
}
function clearView(){ document.getElementById('log').innerHTML=''; }
loadLogs();
setInterval(loadLogs, 3000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Отключаем стандартный лог запросов

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/logs":
            logs = []
            if LOG_FILE.exists():
                for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
                    try:
                        logs.append(json.loads(line))
                    except Exception:
                        pass
            self.send_json(logs)
            return

        if path == "/debug":
            self.send_html(DEBUG_UI)
            return

        # Статические файлы
        if path == "/":
            path = "/index.html"
        file_path = ROOT_DIR / path.lstrip("/")
        if file_path.exists() and file_path.is_file():
            mime, _ = mimetypes.guess_type(str(file_path))
            body = file_path.read_bytes()
            # Инжектим browser.js в HTML автоматически
            if mime == "text/html":
                inject = b'<script src="/debug-browser.js"></script>'
                body = body.replace(b"</head>", inject + b"\n</head>", 1)
                if b"</head>" not in body:
                    body = inject + b"\n" + body
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/debug-browser.js":
            js_path = pathlib.Path(__file__).parent / "browser.js"
            if js_path.exists():
                body = js_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"

        if path in ("/api/log", "/api/cli"):
            try:
                entry = json.loads(body)
            except Exception:
                entry = {"message": body.decode(errors="replace")}
            entry["source"] = "browser" if path == "/api/log" else "cli"
            write_log(entry)
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"🐛 Debug server: http://localhost:{PORT}")
    print(f"   App:   http://localhost:{PORT}/")
    print(f"   Logs:  http://localhost:{PORT}/debug")
    print(f"   Logs file: {LOG_FILE}")
    print(f"   Serving files from: {ROOT_DIR}")
    HTTPServer(("", PORT), Handler).serve_forever()
