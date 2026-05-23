# ReelTrends — инструкции для Claude

## Стек проекта
- Frontend: чистый HTML/JS/CSS (`index.html`)
- Backend/Scrapers: Python 3 (`scraper.py`, `vk_scraper.py`, `youtube_scraper.py`)
- Данные: JSON файлы (`trends.json`, `vk.json`, `youtube.json`, etc.)
- Зависимости: `httpx` (см. `requirements.txt`)

## Система автоматической отладки

В папке `debug/` находится готовая инфраструктура. Используй её **всегда** при отладке.

### Как запустить Python-скрипт с захватом логов
```bash
python debug/capture.py python scraper.py
python debug/capture.py python vk_scraper.py
python debug/capture.py python -m pytest
```

### Как запустить сервер для браузерной отладки
```bash
python debug/server.py 8765 .
# Открывает:
#   http://localhost:8765/       — приложение с авто-инжектом логгера
#   http://localhost:8765/debug  — просмотр всех логов
```
Сервер автоматически инжектирует `debug/browser.js` в HTML — никаких ручных изменений не нужно.

### Как читать логи
```bash
python debug/read_logs.py            # все логи
python debug/read_logs.py errors     # только ошибки
python debug/read_logs.py summary    # сводка
python debug/read_logs.py tail       # следить в реальном времени
python debug/read_logs.py clear      # очистить
```

Лог-файл: `debug/logs/unified.jsonl`

## Алгоритм отладки (выполняй сам, без вопросов к пользователю)

1. **Воспроизвести ошибку** — запусти проблемный скрипт через `debug/capture.py`
2. **Прочитать логи** — `python debug/read_logs.py errors`
3. **Найти файл/строку** — в stack trace будет путь и номер строки
4. **Прочитать контекст** — открой файл, посмотри ±10 строк вокруг ошибки
5. **Исправить** — сделай минимальное изменение
6. **Проверить** — запусти снова через `debug/capture.py`, убедись что ошибка прошла
7. **Повторить** — если новая ошибка, вернись к шагу 2
8. **Зафиксировать** — только когда всё работает, делай commit

## Правила работы с кодом

- Всегда запускай скрипты через `debug/capture.py` — так логи сохраняются
- При ошибке читай `debug/logs/unified.jsonl` — там полный stack trace
- Не спрашивай пользователя о технических деталях — разбирайся сам
- Максимум 7 итераций fix→test на одну проблему; если не решилось — тогда спроси
- Коммит только рабочего кода

## Установка зависимостей
```bash
pip install httpx
```
