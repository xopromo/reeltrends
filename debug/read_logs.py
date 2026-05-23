#!/usr/bin/env python3
"""
debug/read_logs.py — читает и показывает логи для отладки.

Режимы:
  python debug/read_logs.py           — показать все логи
  python debug/read_logs.py errors    — только ошибки
  python debug/read_logs.py tail      — следить в реальном времени (tail -f)
  python debug/read_logs.py clear     — очистить лог-файл
  python debug/read_logs.py summary   — краткая сводка
"""

import sys
import json
import time
import pathlib

LOG_FILE = pathlib.Path(__file__).parent / "logs" / "unified.jsonl"

COLORS = {
    "error":   "\033[91m",  # red
    "warn":    "\033[93m",  # yellow
    "warning": "\033[93m",
    "info":    "\033[96m",  # cyan
    "debug":   "\033[90m",  # gray
}
RESET = "\033[0m"


def color(level: str, text: str) -> str:
    return COLORS.get(level.lower(), "") + text + RESET


def format_entry(e: dict) -> str:
    ts    = (e.get("ts") or "")[:19].replace("T", " ")
    level = (e.get("level") or "info").upper()[:5].ljust(5)
    src   = (e.get("source") or "?")[:8].ljust(8)
    msg   = e.get("message") or ""
    line  = f"{ts} {color(e.get('level','info'), level)} [{src}] {msg}"
    if e.get("stack"):
        line += "\n" + "\n".join("  " + l for l in e["stack"].splitlines()[:5])
    return line


def load_logs(filter_level: str = None) -> list:
    if not LOG_FILE.exists():
        return []
    logs = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if filter_level and e.get("level", "").lower() != filter_level:
                continue
            logs.append(e)
        except Exception:
            pass
    return logs


mode = sys.argv[1] if len(sys.argv) > 1 else "all"

if mode == "clear":
    LOG_FILE.write_text("", encoding="utf-8")
    print("✓ Лог очищен")

elif mode == "errors":
    logs = load_logs("error")
    if not logs:
        print("Ошибок не найдено ✓")
    for e in logs:
        print(format_entry(e))

elif mode == "tail":
    print(f"Следим за {LOG_FILE} (Ctrl+C для выхода)")
    offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    try:
        while True:
            if LOG_FILE.exists():
                size = LOG_FILE.stat().st_size
                if size > offset:
                    with open(LOG_FILE, encoding="utf-8") as f:
                        f.seek(offset)
                        for raw in f:
                            try:
                                e = json.loads(raw)
                                print(format_entry(e))
                            except Exception:
                                pass
                    offset = size
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nСтоп.")

elif mode == "summary":
    logs = load_logs()
    from collections import Counter
    counts = Counter(e.get("level", "info").lower() for e in logs)
    sources = Counter(e.get("source", "?") for e in logs)
    print(f"Всего записей: {len(logs)}")
    print(f"По уровню:     {dict(counts)}")
    print(f"По источнику:  {dict(sources)}")
    errors = [e for e in logs if e.get("level", "").lower() == "error"]
    if errors:
        print(f"\nПоследние {min(5, len(errors))} ошибок:")
        for e in errors[-5:]:
            print("  " + format_entry(e))

else:  # all
    logs = load_logs()
    if not logs:
        print("Логов пока нет. Запусти debug/server.py или debug/capture.py")
    for e in logs:
        print(format_entry(e))
