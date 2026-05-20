# ReelTrends 🏴

Автоматический трекер вирусных рилсов. Работает полностью на GitHub — никаких серверов.

## Как работает

- **GitHub Actions** запускает `scraper.py` каждые 30 минут
- Скрапер собирает тренды с piratex.ai и сохраняет в `trends.json`
- **GitHub Pages** отдаёт `index.html` который читает этот JSON
- Полностью бесплатно, работает 24/7

## Установка (5 минут)

### 1. Включи GitHub Pages
Репо → Settings → Pages → Source: **Deploy from branch** → Branch: **main** → папка: **/ (root)**

Сохрани. Через минуту сайт будет доступен по адресу:
`https://ТВОЙаккаунт.github.io/reeltrends`

### 2. Запусти первый сбор данных
Репо → Actions → **Scrape Trends** → **Run workflow** → Run

Через 3-5 минут в репо появится `trends.json` и на сайте появятся рилсы.

Дальше Actions запускается автоматически каждые 30 минут.

## Файлы

| Файл | Что делает |
|---|---|
| `scraper.py` | Собирает данные с piratex.ai |
| `index.html` | Веб-интерфейс |
| `requirements.txt` | Зависимости Python (только httpx) |
| `.github/workflows/scrape.yml` | Расписание запуска |
| `trends.json` | Данные (создаётся автоматически) |
