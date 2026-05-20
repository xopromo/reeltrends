# ReelTrends 🏴

Клон трендвотчинга piratex.ai — собирает вирусные рилсы и показывает их в удобном интерфейсе.

## Как запустить локально

### Требования
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac/Linux)
- Git

### Запуск одной командой

```bash
git clone https://github.com/ВАШ_АККАУНТ/reeltrends
cd reeltrends
docker-compose up
```

Открыть в браузере: **http://localhost:8000**

При первом запуске автоматически:
1. Поднимется PostgreSQL
2. Создадутся таблицы
3. Запустится первый сбор данных (~2-3 минуты)
4. Каждые 30 минут данные обновляются автоматически

### Ручной запуск сбора

Полный сбор всех 600+ рилсов:
```bash
docker-compose exec app python scraper.py full
```

Быстрый инкрементальный (только новые тренды):
```bash
docker-compose exec app python scraper.py incremental
```

Или через API:
```bash
curl -X POST http://localhost:8000/api/scrape
```

## API

| Endpoint | Описание |
|---|---|
| `GET /api/trends` | Список трендов |
| `GET /api/niches` | Список ниш |
| `GET /api/status` | Статус и статистика |
| `POST /api/scrape` | Запустить сбор вручную |

### Параметры /api/trends

| Параметр | Значения | По умолчанию |
|---|---|---|
| `sort` | `hot_score`, `x_factor`, `views`, `recent` | `hot_score` |
| `platform` | `instagram`, `youtube`, `tiktok`, `` | `` (все) |
| `days` | `1`, `7`, `30`, `365` | все время |
| `niche` | строка или несколько через запятую | все |
| `page` | число | `1` |
| `per_page` | до 100 | `20` |
| `q` | текст поиска | `` |

## Деплой на Railway (бесплатно, 24/7)

1. Создать аккаунт на [railway.app](https://railway.app)
2. New Project → Deploy from GitHub → выбрать этот репо
3. Добавить PostgreSQL: New Service → Database → PostgreSQL
4. В переменных окружения приложения добавить:
   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```
5. Готово — Railway сам задеплоит и запустит

## Структура проекта

```
reeltrends/
├── scraper.py        # сбор данных с piratex.ai
├── db.py             # PostgreSQL операции
├── app.py            # FastAPI + планировщик
├── frontend/
│   └── index.html    # веб-интерфейс
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

## Метрики

- **X-factor** = просмотры рилса / медиана просмотров автора
- **Velocity** = прирост просмотров в час
- **Hot score** = engagement с учётом свежести контента
- **CR** = comments / views × 100%
