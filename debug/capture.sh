#!/usr/bin/env bash
# debug/capture.sh — обёртка для запуска любой команды с захватом логов.
#
# Использование: ./debug/capture.sh <команда> [аргументы...]
# Пример:        ./debug/capture.sh python scraper.py
#                ./debug/capture.sh npm start
#                ./debug/capture.sh python -m pytest
#
# Все stdout/stderr пишутся в debug/logs/unified.jsonl и выводятся в терминал.

set -euo pipefail

LOG_FILE="$(dirname "$0")/logs/unified.jsonl"
LOG_API="http://localhost:8765/api/cli"
mkdir -p "$(dirname "$LOG_FILE")"

if [ $# -eq 0 ]; then
  echo "Использование: $0 <команда> [аргументы...]" >&2
  exit 1
fi

CMD="$*"
START_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

log_entry() {
  local level="$1"
  local message="$2"
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  local entry
  entry=$(printf '{"ts":"%s","level":"%s","source":"cli","cmd":"%s","message":%s}' \
    "$ts" "$level" "$CMD" "$(echo "$message" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip()))' 2>/dev/null || echo '"'"$message"'"')")
  echo "$entry" >> "$LOG_FILE"
  # Пробуем отправить на сервер если запущен
  curl -s -X POST "$LOG_API" \
    -H "Content-Type: application/json" \
    -d "$entry" > /dev/null 2>&1 || true
}

log_entry "info" "▶ START: $CMD"
echo "▶ Запуск: $CMD"
echo "  Лог: $LOG_FILE"
echo ""

# Запускаем команду, перехватываем stdout и stderr раздельно
EXIT_CODE=0
{
  "$@" 2> >(while IFS= read -r line; do
    echo "$line" >&2
    log_entry "error" "$line"
  done)
} | while IFS= read -r line; do
  echo "$line"
  log_entry "info" "$line"
done || EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  log_entry "error" "✗ FAILED (exit $EXIT_CODE): $CMD"
  echo ""
  echo "✗ Команда завершилась с ошибкой: exit $EXIT_CODE"
else
  log_entry "info" "✓ SUCCESS: $CMD"
  echo ""
  echo "✓ Готово"
fi

exit $EXIT_CODE
