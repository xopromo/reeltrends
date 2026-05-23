#!/usr/bin/env python3
"""
debug/capture.py — Python-обёртка для запуска любой команды с захватом логов.

Использование: python debug/capture.py <команда> [аргументы...]
Пример:        python debug/capture.py python scraper.py
               python debug/capture.py python -m pytest

Лучше .sh потому что корректно работает на Windows и macOS.
"""

import sys
import json
import subprocess
import datetime
import threading
import pathlib
import urllib.request
import urllib.error

if len(sys.argv) < 2:
    print("Использование: python debug/capture.py <команда> [аргументы...]")
    sys.exit(1)

LOG_FILE = pathlib.Path(__file__).parent / "logs" / "unified.jsonl"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_API  = "http://localhost:8765/api/cli"
CMD      = sys.argv[1:]


def ts():
    return datetime.datetime.utcnow().isoformat() + "Z"


def write_log(level: str, message: str):
    entry = {
        "ts": ts(),
        "level": level,
        "source": "cli",
        "cmd": " ".join(CMD),
        "message": message,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Пробуем отправить на сервер (если запущен)
    try:
        req = urllib.request.Request(
            LOG_API,
            data=json.dumps(entry).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass


def stream_output(pipe, level: str, prefix: str = ""):
    for raw_line in iter(pipe.readline, b""):
        line = raw_line.decode(errors="replace").rstrip()
        if level == "info":
            print(prefix + line)
        else:
            print(prefix + line, file=sys.stderr)
        write_log(level, line)
    pipe.close()


write_log("info", f"▶ START: {' '.join(CMD)}")
print(f"▶ Запуск: {' '.join(CMD)}")
print(f"  Лог: {LOG_FILE}\n")

proc = subprocess.Popen(
    CMD,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

t_out = threading.Thread(target=stream_output, args=(proc.stdout, "info"))
t_err = threading.Thread(target=stream_output, args=(proc.stderr, "error", "ERR "))
t_out.start()
t_err.start()
t_out.join()
t_err.join()
proc.wait()

if proc.returncode != 0:
    write_log("error", f"✗ FAILED (exit {proc.returncode}): {' '.join(CMD)}")
    print(f"\n✗ Команда завершилась с ошибкой: exit {proc.returncode}", file=sys.stderr)
else:
    write_log("info", f"✓ SUCCESS: {' '.join(CMD)}")
    print(f"\n✓ Готово")

sys.exit(proc.returncode)
