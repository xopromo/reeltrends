"""
YouTube Shorts scraper с умным управлением базой каналов.

Логика роста базы:
1. Каждый запуск — мониторим существующие каналы
2. Вирусные видео (x_factor > 5) → ищем похожие каналы
3. Каждые 7 запусков — добавляем новые каналы по поисковым запросам
4. Каналы с хорошей историей (score > 0) никогда не удаляются
5. Слабые каналы (score < -3) вытесняются новыми
"""

import asyncio
import httpx
import json
import os
import re
import statistics
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
BASE = "https://www.googleapis.com/youtube/v3"
OUTPUT_FILE = "youtube.json"
CHANNELS_FILE = "yt_channels.json"
MAX_CHANNELS = int(os.environ.get("YT_MAX_CHANNELS") or "1000")  # регулируется через GitHub Secret
NIGHT_RUN = os.environ.get("YT_NIGHT_RUN", "false").lower() == "true"
MANUAL_RUN = os.environ.get("YT_MANUAL_RUN", "false").lower() == "true"  # ночной дообор
QUOTA_LIMIT = 10_000

# Стоимость каждого типа запроса в units
QUOTA_COST = {
    "search": 100,
    "channels": 1,
    "playlistItems": 1,
    "videos": 1,
}

quota_used = 0  # глобальный счётчик текущего прогона
QUOTA_FILE = "yt_quota.json"
QUOTA_SAFETY_BUFFER = 0.20   # резерв 20% — не тратим последние 2000 units
QUOTA_OVERESTIMATE  = 1.20   # считаем остаток на 20% оптимистичнее расчёта


# ── Quota persistence ──────────────────────────────────────────────

def load_quota_log() -> dict:
    """Загружаем дневной лог квоты. Сбрасываем если новый день UTC."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except Exception:
            pass
    return {"date": today, "runs": [], "total_used": 0}


def save_quota_log(log: dict, run_cost: int):
    """Добавляем текущий прогон в лог."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        log["runs"].append({"time": now, "units": run_cost})
        log["total_used"] = sum(r["units"] for r in log["runs"])
        atomic_write(QUOTA_FILE, log)
    except Exception as e:
        print(f"  ⚠️ Не удалось сохранить лог квоты: {e}")


def calc_night_budget(log: dict) -> int:
    """
    Считаем сколько units можно потратить в ночном прогоне.
    Берём расчётный остаток и умножаем на QUOTA_OVERESTIMATE (оптимизм +20%),
    но оставляем QUOTA_SAFETY_BUFFER нетронутым.
    """
    safe_limit = int(QUOTA_LIMIT * (1 - QUOTA_SAFETY_BUFFER))  # 8000
    spent_today = log.get("total_used", 0)
    raw_remaining = QUOTA_LIMIT - spent_today
    # Оптимистичная оценка: реальный остаток может быть выше на 20%
    optimistic_remaining = int(raw_remaining * QUOTA_OVERESTIMATE)
    budget = min(optimistic_remaining, safe_limit - spent_today)
    budget = max(budget, 0)
    print(f"🌙 Квота: потрачено сегодня ~{spent_today}, расчётный остаток ~{raw_remaining}")
    print(f"   Оптимистичный бюджет для ночного прогона: {budget} units")
    return budget


def quota_remaining() -> int:
    """Сколько units ещё можно потратить в этом прогоне."""
    safe_limit = int(QUOTA_LIMIT * (1 - QUOTA_SAFETY_BUFFER))
    return max(0, safe_limit - quota_used)


def quota_ok(expected_cost: int = 1) -> bool:
    """Можно ли сделать ещё запрос? Проверяем с запасом."""
    return quota_remaining() >= expected_cost

SEARCH_QUERIES = [
    {"q": "фитнес shorts", "lang": "ru", "region": "RU"},
    {"q": "недвижимость shorts", "lang": "ru", "region": "RU"},
    {"q": "юрист shorts", "lang": "ru", "region": "RU"},
    {"q": "маркетинг shorts", "lang": "ru", "region": "RU"},
    {"q": "психология shorts", "lang": "ru", "region": "RU"},
    {"q": "бизнес shorts", "lang": "ru", "region": "RU"},
    {"q": "финансы shorts", "lang": "ru", "region": "RU"},
    {"q": "путешествия shorts", "lang": "ru", "region": "RU"},
    {"q": "юмор пранк shorts", "lang": "ru", "region": "RU"},
    {"q": "авто shorts", "lang": "ru", "region": "RU"},
    {"q": "кулинария рецепты shorts", "lang": "ru", "region": "RU"},
    {"q": "саморазвитие shorts", "lang": "ru", "region": "RU"},
    {"q": "fitness shorts viral", "lang": "en", "region": "US"},
    {"q": "motivation shorts viral", "lang": "en", "region": "US"},
    {"q": "funny shorts viral", "lang": "en", "region": "US"},
    {"q": "life hack shorts viral", "lang": "en", "region": "US"},
    {"q": "tech shorts viral", "lang": "en", "region": "US"},
    {"q": "finance money shorts", "lang": "en", "region": "US"},
    {"q": "cooking recipe shorts", "lang": "en", "region": "US"},
    {"q": "travel shorts viral", "lang": "en", "region": "US"},
]


# ── Helpers ────────────────────────────────────────────────────────

def parse_duration(iso: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    return int(m.group(1) or 0)*3600 + int(m.group(2) or 0)*60 + int(m.group(3) or 0)


def is_short(duration_iso: str, title: str, description: str) -> bool:
    secs = parse_duration(duration_iso)
    has_tag = "#shorts" in (title+description).lower() or "#short" in (title+description).lower()
    return secs <= 180 and (has_tag or secs <= 60)


def calc_velocity(views: int, published_at: str) -> float:
    try:
        pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        hours = max(0.5, (datetime.now(timezone.utc) - pub).total_seconds() / 3600)
        return round(views / hours, 1)
    except Exception:
        return 0.0


class QuotaExceededError(Exception):
    pass


async def api_get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    global quota_used
    cost = QUOTA_COST.get(path, 1)
    if not quota_ok(cost):
        raise QuotaExceededError(f"Квота заканчивается, пропускаем запрос ({quota_used} использовано)")
    params["key"] = API_KEY
    r = await client.get(f"{BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    quota_used += cost
    return r.json()


def print_quota():
    remaining = QUOTA_LIMIT - quota_used
    pct = quota_used / QUOTA_LIMIT * 100
    print(f"📊 API quota: {quota_used}/{QUOTA_LIMIT} units ({pct:.1f}%), осталось {remaining}")


# ── Channel store ──────────────────────────────────────────────────

def load_channels() -> dict:
    """
    Структура канала:
    {
      id, title, lang, niche,
      score: float,        # накопленный рейтинг (-inf..+inf)
      viral_count: int,    # сколько раз давал x_factor > 5
      last_seen: str,      # когда последний раз давал видео
      added_at: str
    }
    """
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def atomic_write(path: str, data):
    """Атомарная запись через временный файл — защита от повреждения при краше."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # атомарная операция на всех ОС
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def save_channels(channels: dict):
    atomic_write(CHANNELS_FILE, channels)
    print(f"  ✓ Channels saved: {len(channels)}")


def update_channel_score(channels: dict, cid: str, shorts_found: list):
    """Обновляем рейтинг канала и накапливаем историю просмотров для медианы."""
    if cid not in channels:
        return
    ch = channels[cid]

    if not shorts_found:
        ch["score"] = round(ch.get("score", 0) - 0.5, 1)
        return

    viral = [s for s in shorts_found if (s.get("x_factor") or 0) >= 5]
    ch["viral_count"] = ch.get("viral_count", 0) + len(viral)
    ch["last_seen"] = datetime.now(timezone.utc).isoformat()

    # Накапливаем историю просмотров для стабильной медианы
    # Храним последние 50 значений — достаточно для статистики
    views_history = ch.get("views_history", [])
    views_history.extend(s.get("views", 0) for s in shorts_found if s.get("views", 0) > 0)
    ch["views_history"] = views_history[-50:]

    # Обновляем медиану из накопленной истории
    if ch["views_history"]:
        ch["median_views"] = round(statistics.median(ch["views_history"]))

    delta = len(viral) * 2 + (len(shorts_found) - len(viral)) * 0.5
    ch["score"] = round(ch.get("score", 0) + delta, 1)


def get_channel_tier(ch: dict) -> int:
    """
    Тир определяет как часто проверяем канал:
    0 (топ, score>2)    — каждый прогон
    1 (средние, 0..2)   — каждый 2й прогон
    2 (слабые, <0)      — каждый 4й прогон
    """
    score = ch.get("score", 0)
    if score > 2:
        return 0
    if score >= 0:
        return 1
    return 2


TIER_INTERVAL = {0: 1, 1: 2, 2: 4}  # каждые N прогонов


def should_check_channel(ch: dict, run_count: int) -> bool:
    """Нужно ли проверять канал в этом прогоне?"""
    tier = get_channel_tier(ch)
    interval = TIER_INTERVAL[tier]
    # Новые каналы (нет last_seen) — всегда проверяем первый раз
    if not ch.get("last_seen"):
        return True
    return run_count % interval == 0


def prune_channels(channels: dict) -> dict:
    """Удаляем слабые каналы если база переполнена."""
    if len(channels) <= MAX_CHANNELS:
        return channels

    # Сортируем по score — защищаем хорошие каналы
    sorted_ch = sorted(channels.values(), key=lambda x: x.get("score", 0), reverse=True)

    # Оставляем топ MAX_CHANNELS
    kept = {ch["id"]: ch for ch in sorted_ch[:MAX_CHANNELS]}
    removed = len(channels) - len(kept)
    if removed > 0:
        print(f"  Pruned {removed} weak channels (score < {sorted_ch[MAX_CHANNELS].get('score', 0):.1f})")
    return kept


# ── Discovery ──────────────────────────────────────────────────────

async def discover_new_channels(client: httpx.AsyncClient, channels: dict, queries: list) -> int:
    """Ищет новые каналы по поисковым запросам."""
    added = 0
    now = datetime.now(timezone.utc).isoformat()

    for query in queries:
        try:
            data = await api_get(client, "search", {
                "part": "snippet",
                "type": "channel",
                "q": query["q"],
                "relevanceLanguage": query["lang"],
                "regionCode": query["region"],
                "maxResults": 10,
            })
            for item in data.get("items", []):
                cid = item["id"]["channelId"]
                if cid not in channels:
                    channels[cid] = {
                        "id": cid,
                        "title": item["snippet"]["channelTitle"],
                        "lang": query["lang"],
                        "niche": query["q"].split()[0],
                        "score": 0.0,
                        "viral_count": 0,
                        "last_seen": None,
                        "added_at": now,
                    }
                    added += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"  Search failed: {query['q']}: {e}")

    print(f"  Discovered {added} new channels from search")
    return added


async def discover_channels_via_shorts(client: httpx.AsyncClient, channels: dict, queries: list) -> int:
    """Ищет каналы через популярные Shorts — надёжнее channel search."""
    added = 0
    now = datetime.now(timezone.utc).isoformat()
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for query in queries[:10]:  # первые 10 запросов
        try:
            data = await api_get(client, "search", {
                "part": "snippet",
                "type": "video",
                "q": query["q"] + " shorts",
                "videoDuration": "short",
                "order": "viewCount",
                "publishedAfter": since,
                "relevanceLanguage": query["lang"],
                "maxResults": 10,
            })
            for item in data.get("items", []):
                cid = item["snippet"].get("channelId")
                if cid and cid not in channels:
                    channels[cid] = {
                        "id": cid,
                        "title": item["snippet"].get("channelTitle", ""),
                        "lang": query["lang"],
                        "niche": query["q"].split()[0],
                        "score": 1.0,  # бонус — найден через популярное видео
                        "viral_count": 0,
                        "last_seen": None,
                        "added_at": now,
                    }
                    added += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"  Video search failed: {query['q']}: {e}")

    print(f"  Discovered {added} new channels via Shorts search")
    return added


async def discover_related_channels(client: httpx.AsyncClient, channels: dict, viral_video_ids: list) -> int:
    """
    Ищет каналы похожие на вирусные видео через relatedToVideoId.

    ВНИМАНИЕ: relatedToVideoId deprecated с 2023 — часто возвращает
    пустые результаты или ошибку 400/403. Полностью защищена:
    - ошибки API не роняют прогон
    - если метод не работает — детектируем и прекращаем тратить квоту
    - каждый запрос = 100 units, лимитируем до 5 попыток
    """
    if not viral_video_ids:
        return 0

    added = 0
    failed = 0
    empty = 0
    now = datetime.now(timezone.utc).isoformat()
    MAX_ATTEMPTS = 5
    MAX_FAILURES = 2

    print(f"  Trying relatedToVideoId for {min(len(viral_video_ids), MAX_ATTEMPTS)} viral videos...")

    for vid in viral_video_ids[:MAX_ATTEMPTS]:
        if not quota_ok(100):
            print(f"  Недостаточно квоты для relatedToVideoId — пропускаем")
            break
        if failed >= MAX_FAILURES:
            print(f"  relatedToVideoId вернул {failed} ошибок — метод deprecated, прекращаем")
            break
        try:
            data = await api_get(client, "search", {
                "part": "snippet",
                "type": "channel",
                "relatedToVideoId": vid,
                "maxResults": 5,
            })
            items = data.get("items", [])
            if not items:
                empty += 1
                if empty >= MAX_ATTEMPTS // 2:
                    print(f"  relatedToVideoId пустые результаты ({empty}/{MAX_ATTEMPTS}) — прекращаем")
                    break
                await asyncio.sleep(0.3)
                continue
            failed = 0
            for item in items:
                cid = item.get("id", {}).get("channelId") or item.get("snippet", {}).get("channelId")
                if cid and cid not in channels:
                    snippet_data = item.get("snippet", {})
                    title = snippet_data.get("channelTitle", "")
                    # Пытаемся определить язык из локализации описания
                    # Будет уточнён при первом реальном прогоне канала
                    default_lang = snippet_data.get("defaultLanguage", "") or                                    snippet_data.get("country", "")
                    lang = "ru" if default_lang in ("RU", "BY", "KZ", "UA") else                            "en" if default_lang in ("US", "GB", "CA", "AU") else "unknown"
                    channels[cid] = {
                        "id": cid,
                        "title": title,
                        "lang": lang,
                        "niche": "related",   # уточнится после первого прогона
                        "score": 1.0,
                        "viral_count": 0,
                        "last_seen": None,
                        "added_at": now,
                    }
                    added += 1
            await asyncio.sleep(0.3)
        except QuotaExceededError:
            print(f"  Квота — останавливаем relatedToVideoId")
            break
        except Exception as e:
            failed += 1
            err_str = str(e)
            if "400" in err_str or "403" in err_str or "deprecated" in err_str.lower():
                print(f"  relatedToVideoId не поддерживается ({e}) — прекращаем")
                break
            print(f"  relatedToVideoId failed for {vid}: {e} — пробуем следующий")
            await asyncio.sleep(0.5)

    if added > 0:
        print(f"  Discovered {added} related channels from viral videos")
    else:
        print(f"  relatedToVideoId: 0 каналов (empty={empty}, errors={failed})")
    return added


# ── Main scrape ────────────────────────────────────────────────────

async def get_channel_stats(client: httpx.AsyncClient, channel_ids: list) -> dict:
    stats = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i+50]
        try:
            data = await api_get(client, "channels", {
                "part": "statistics,contentDetails",
                "id": ",".join(batch),
            })
            for item in data.get("items", []):
                cid = item["id"]
                s = item.get("statistics", {})
                stats[cid] = {
                    "subscribers": int(s.get("subscriberCount", 0)),
                    "uploads_playlist": item.get("contentDetails", {})
                        .get("relatedPlaylists", {}).get("uploads", ""),
                }
            await asyncio.sleep(0.2)
        except QuotaExceededError as e:
            print(f"  ⚠️ {e} — останавливаем get_channel_stats досрочно ({len(stats)} каналов обработано)")
            break
        except Exception as e:
            print(f"  ⚠️ Channel stats batch failed: {e} — продолжаем")
    return stats


async def get_recent_videos(client: httpx.AsyncClient, playlist_id: str, max_results: int = 5, days: int = 7) -> list:
    if not playlist_id:
        return []
    try:
        data = await api_get(client, "playlistItems", {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": max_results,
        })
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = []
        for item in data.get("items", []):
            pub = item["snippet"].get("publishedAt", "")
            try:
                if datetime.fromisoformat(pub.replace("Z", "+00:00")) < cutoff:
                    continue
            except Exception:
                continue
            result.append({
                "video_id": item["snippet"]["resourceId"]["videoId"],
                "published_at": pub,
            })
        return result
    except QuotaExceededError:
        raise  # пробрасываем выше — вызывающий решает
    except Exception:
        return []


async def get_video_details(client: httpx.AsyncClient, video_ids: list) -> list:
    results = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        try:
            data = await api_get(client, "videos", {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
            })
            results.extend(data.get("items", []))
            await asyncio.sleep(0.2)
        except QuotaExceededError as e:
            print(f"  ⚠️ {e} — останавливаем get_video_details досрочно ({len(results)} видео получено)")
            break
        except Exception as e:
            print(f"  ⚠️ Video details batch failed: {e} — продолжаем")
    return results


async def scrape_youtube() -> list:
    if not API_KEY:
        print("❌ YOUTUBE_API_KEY не задан!")
        return []

    # Загружаем дневной лог квоты
    quota_log = load_quota_log()

    async with httpx.AsyncClient(timeout=30) as client:

        # 1. Загружаем базу каналов
        channels = load_channels()
        # run_count — реальный накопленный счётчик прогонов (не зависит от размера базы)
        run_count = channels.get("__meta__", {}).get("run_count", 0) + 1
        # Сохраняем метаданные отдельно от каналов
        if "__meta__" not in channels:
            channels["__meta__"] = {}
        channels["__meta__"]["run_count"] = run_count
        # Рабочий список каналов без метаданных
        real_channels = {k: v for k, v in channels.items() if k != "__meta__"}
        print(f"✓ Loaded {len(real_channels)} channels (run #{run_count})")

        # 2. Поиск новых каналов
        if NIGHT_RUN:
            night_budget = calc_night_budget(quota_log)
            print("🌙 Ночной дообор — расширенный поиск каналов...")
            try:
                added = await discover_new_channels(client, real_channels, SEARCH_QUERIES)
                added += await discover_channels_via_shorts(client, real_channels, SEARCH_QUERIES)
            except QuotaExceededError as e:
                print(f"  ⚠️ {e} — поиск каналов прерван, продолжаем с тем что есть")

            # Поиск похожих каналов через вирусные видео (только ночью, quota permitting)
            # relatedToVideoId deprecated — защита внутри функции
            if quota_ok(100):
                try:
                    existing_items = load_existing()
                    viral_ids = [
                        v["id"] for v in existing_items
                        if (v.get("x_factor") or 0) >= 5
                    ][:10]
                    if viral_ids:
                        await discover_related_channels(client, real_channels, viral_ids)
                    else:
                        print("  relatedToVideoId: нет вирусных видео в базе — пропускаем")
                except Exception as e:
                    print(f"  ⚠️ discover_related_channels упал: {e} — продолжаем")
            else:
                print("  relatedToVideoId: недостаточно квоты — пропускаем")

            channels = prune_channels(channels)
            save_channels(channels)
        elif MANUAL_RUN or run_count % 3 == 0 or len(channels) < 100:
            print("🔍 Discovering new channels from search queries...")
            try:
                added = await discover_new_channels(client, real_channels, SEARCH_QUERIES)
                added += await discover_channels_via_shorts(client, real_channels, SEARCH_QUERIES)
            except QuotaExceededError as e:
                print(f"  ⚠️ {e} — поиск каналов прерван, продолжаем с тем что есть")
            real_channels = prune_channels(real_channels)
            channels = {**real_channels, "__meta__": channels["__meta__"]}
            save_channels(channels)

        all_channel_ids = list(real_channels.keys())

        # Тиринг: фильтруем каналы по частоте проверки
        channel_ids = [cid for cid in all_channel_ids
                       if should_check_channel(real_channels[cid], run_count)]
        skipped_by_tier = len(all_channel_ids) - len(channel_ids)
        if skipped_by_tier:
            print(f"  Тиринг: проверяем {len(channel_ids)}/{len(all_channel_ids)} каналов "
                  f"(пропущено {skipped_by_tier} по тиру)")

        # 3. Статистика каналов (только для тех что проверяем)
        print(f"📊 Getting channel stats for {len(channel_ids)} channels...")
        try:
            chan_stats = await get_channel_stats(client, channel_ids)
        except Exception as e:
            print(f"  ⚠️ get_channel_stats полностью упал: {e} — продолжаем с пустой статистикой")
            chan_stats = {}

        # 4. Свежие видео с каналов
        # Окно сбора зависит от того новый канал или нет:
        #   новый (нет last_seen) → 7 дней (полное знакомство)
        #   известный обычный прогон → 26 часов (с перекрытием на задержки)
        #   ночной прогон → 48 часов (покрываем пропуски за день)
        now_utc = datetime.now(timezone.utc)

        if NIGHT_RUN:
            # Бюджетный расчёт для ночного прогона
            remaining = quota_remaining()
            cost_per_channel = 3  # 1 playlistItems + ~2 videos батча
            max_channels_by_quota = max(50, remaining // cost_per_channel)
            if len(channel_ids) > max_channels_by_quota:
                print(f"  Бюджет позволяет ~{max_channels_by_quota} каналов из {len(channel_ids)}, приоритет — лучшие по score")
                sorted_ids = sorted(channel_ids, key=lambda c: real_channels.get(c, {}).get("score", 0), reverse=True)
                channel_ids = sorted_ids[:max_channels_by_quota]

        mode_label = "ночной" if NIGHT_RUN else "обычный"
        print(f"📥 Fetching recent videos ({mode_label})...")
        all_video_ids = []
        video_to_channel = {}

        quota_stopped = False
        for cid in channel_ids:
            if not quota_ok(2):
                print(f"  ⚠️ Квота на исходе — останавливаем сбор видео досрочно ({len(all_video_ids)} видео собрано)")
                quota_stopped = True
                break

            ch = real_channels.get(cid, {})
            is_new_channel = not ch.get("last_seen")

            # Умное окно: новый канал — 7 дней, иначе 26ч или 48ч ночью
            if is_new_channel:
                cutoff_days = 7
                max_results = 10
            elif NIGHT_RUN:
                cutoff_days = 2   # 48 часов
                max_results = 5
            else:
                cutoff_days = 2   # 26 часов — используем фильтр по времени ниже
                max_results = 3   # каналы редко дают >2 Shorts в сутки

            cutoff_dt = now_utc - timedelta(hours=26 if not is_new_channel and not NIGHT_RUN else cutoff_days * 24)

            playlist_id = chan_stats.get(cid, {}).get("uploads_playlist", "")
            try:
                recent = await get_recent_videos(client, playlist_id, max_results=max_results, days=cutoff_days)
            except QuotaExceededError as e:
                print(f"  ⚠️ {e} — останавливаем сбор видео")
                quota_stopped = True
                break
            except Exception as e:
                print(f"  ⚠️ Ошибка канала {cid}: {e} — пропускаем")
                continue

            for v in recent:
                try:
                    pub = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
                    if pub < cutoff_dt:
                        continue
                except Exception:
                    continue
                all_video_ids.append(v["video_id"])
                video_to_channel[v["video_id"]] = cid
            await asyncio.sleep(0.1)

        print(f"  Found {len(all_video_ids)} recent videos")
        if not all_video_ids:
            return []

        # 5. Детали видео
        print(f"📈 Getting video details...")
        videos = await get_video_details(client, all_video_ids)

        # 6. Фильтруем Shorts и считаем метрики
        shorts = []
        channel_views = {}
        channel_shorts = {}  # cid → [shorts] для обновления score

        for v in videos:
            details = v.get("contentDetails", {})
            snippet = v.get("snippet", {})
            stats = v.get("statistics", {})

            duration = details.get("duration", "")
            title = snippet.get("title", "")
            desc = snippet.get("description", "")

            if not is_short(duration, title, desc):
                continue

            # Фильтр языка — пропускаем если явно указан нежелательный язык
            # Пустая строка = YouTube не определил язык → пропускаем через фильтр
            audio_lang = snippet.get("defaultAudioLanguage", "")[:2]
            ALLOWED_LANGS = {"ru", "en", "uk", "be", "kk", ""}
            if audio_lang not in ALLOWED_LANGS:
                continue

            views = int(stats.get("viewCount", 0))
            # Свежие видео (< 6 часов) не фильтруем по просмотрам —
            # они могут быть ниже порога сейчас но стать вирусными
            pub_raw = snippet.get("publishedAt", "")
            try:
                pub_dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600
            except Exception:
                age_hours = 999
            if views < 1000 and age_hours >= 6:
                continue

            likes = int(stats.get("likeCount", 0))
            comments = int(stats.get("commentCount", 0))
            published_at = snippet.get("publishedAt", "")
            cid = video_to_channel.get(v["id"], "")
            channel_title = snippet.get("channelTitle", "")
            subs = chan_stats.get(cid, {}).get("subscribers", 0)

            if cid not in channel_views:
                channel_views[cid] = []
            channel_views[cid].append(views)

            velocity = calc_velocity(views, published_at)
            niche = real_channels.get(cid, {}).get("niche", "other")
            lang = real_channels.get(cid, {}).get("lang", "en")

            thumb = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumb.get("maxres", {}).get("url") or
                thumb.get("standard", {}).get("url") or
                thumb.get("high", {}).get("url") or ""
            )

            item = {
                "id": v["id"],
                "url": f"https://www.youtube.com/shorts/{v['id']}",
                "title": title,
                "caption": desc[:500],
                "thumbnail_url": thumbnail_url,
                "published_at": published_at,
                "duration_sec": parse_duration(duration),
                "views": views,
                "likes": likes,
                "comments": comments,
                "velocity": velocity,
                "x_factor": None,
                "hot_score": None,
                "platform": "youtube",
                "niche": niche,
                "lang": lang,
                "author": {
                    "channel_id": cid,
                    "username": channel_title,
                    "display_name": channel_title,
                    "platform": "youtube",
                    "followers_count": subs,
                    "median_views": 0,
                },
                "first_seen": datetime.now(timezone.utc).isoformat(),  # когда впервые попало в базу, не меняется
                "stats_updated_at": datetime.now(timezone.utc).isoformat(),  # когда последний раз обновлялась статистика
                "added_at": None,  # заполняется при merge в save()
            }
            shorts.append(item)

            if cid not in channel_shorts:
                channel_shorts[cid] = []
            channel_shorts[cid].append(item)

        # 7. Считаем x_factor и hot_score
        for short in shorts:
            cid = short["author"]["channel_id"]
            ch_data = real_channels.get(cid, {})

            # Предпочитаем историческую медиану канала (накопленная)
            # Fallback на медиану текущего прогона если истории нет
            historical_median = ch_data.get("median_views", 0)
            if historical_median > 0:
                median = historical_median
            else:
                views_list = channel_views.get(cid, [short["views"]])
                median = statistics.median(views_list)

            short["author"]["median_views"] = round(median)

            if median > 0:
                short["x_factor"] = round(short["views"] / median, 2)

            if short["views"] > 0:
                age_h = max(1, (datetime.now(timezone.utc) -
                    datetime.fromisoformat(short["published_at"].replace("Z", "+00:00"))
                ).total_seconds() / 3600)
                engagement = short["likes"] + short["comments"] * 3
                short["hot_score"] = round(engagement / short["views"] / (1 + age_h / 24), 4)

        print(f"✓ Found {len(shorts)} Shorts")

        # 8. Обновляем score каналов
        for cid in channel_ids:
            update_channel_score(real_channels, cid, channel_shorts.get(cid, []))

        # 9. Сохраняем обновлённую базу каналов
        channels = {**real_channels, "__meta__": channels["__meta__"]}
        save_channels(channels)

        # Статистика базы
        good = sum(1 for ch in real_channels.values() if ch.get("score", 0) > 2)
        viral_ch = sum(1 for ch in real_channels.values() if ch.get("viral_count", 0) > 0)
        print(f"  Channel stats: {len(channels)} total, {good} good (score>2), {viral_ch} ever viral")

        shorts.sort(key=lambda x: x.get("hot_score") or 0, reverse=True)
        print_quota()
        return shorts


def get_ttl_days(item: dict) -> float:
    """
    TTL считается от stats_updated_at (последнее обновление статистики),
    а не от даты публикации — чтобы активно растущие видео не выбывали.
    """
    xf    = item.get("x_factor") or 0
    views = item.get("views") or 0
    vel   = item.get("velocity") or 0
    if xf > 10 and views > 500_000: return float("inf")
    if vel > 100 and views > 100_000: return float("inf")
    if xf > 5  and views > 100_000: return 365
    if xf > 2  and views > 10_000:  return 90
    if xf > 1  and views > 5_000:   return 60   # новый промежуточный уровень
    return 30


def prune_old(items: list) -> list:
    now = datetime.now(timezone.utc)
    kept, removed = [], 0
    for item in items:
        ttl = get_ttl_days(item)
        if ttl == float("inf"):
            kept.append(item)
            continue
        # Используем stats_updated_at (последнее обновление статистики), а не published_at
        # Это позволяет видео с растущими показателями оставаться в базе
        ref_str = item.get("stats_updated_at") or item.get("first_seen") or item.get("added_at") or item.get("published_at")
        if not ref_str:
            kept.append(item)
            continue
        try:
            ref = datetime.fromisoformat(ref_str.replace("Z", "+00:00"))
            age_days = (now - ref).total_seconds() / 86400
            if age_days < ttl:
                kept.append(item)
            else:
                removed += 1
        except Exception:
            kept.append(item)
    if removed:
        print(f"  Pruned {removed} old items (kept {len(kept)})")
    return kept


def load_existing() -> list:
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f).get("items", [])
        except Exception:
            pass
    return []


async def refresh_existing_stats(client: httpx.AsyncClient, existing: list) -> dict:
    """
    Обновляем статистику видео которые уже есть в базе но не попали в свежий сбор.
    Стоимость: 1 unit на каждые 50 видео — очень дёшево.

    Частота обновления по возрасту видео:
    - младше 7 дней  → каждый прогон
    - 7–30 дней      → только ночной прогон или ручной
    - старше 30 дней → только ручной прогон
    """
    now = datetime.now(timezone.utc)
    to_refresh = []

    for item in existing:
        vid_id = item.get("id")
        if not vid_id:
            continue
        pub_str = item.get("published_at")
        if not pub_str:
            continue
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            age_days = (now - pub).total_seconds() / 86400
        except Exception:
            continue

        if age_days < 7:
            to_refresh.append(vid_id)          # каждый прогон
        elif age_days < 30 and (NIGHT_RUN or MANUAL_RUN):
            to_refresh.append(vid_id)          # ночной или ручной
        elif MANUAL_RUN:
            to_refresh.append(vid_id)          # только ручной

    if not to_refresh:
        return {}

    print(f"🔄 Refreshing stats for {len(to_refresh)} existing videos...")
    refreshed = {}
    batches_done = 0

    for i in range(0, len(to_refresh), 50):
        if not quota_ok(1):
            print(f"  ⚠️ Квота — останавливаем refresh досрочно ({batches_done} батчей обновлено)")
            break
        batch = to_refresh[i:i+50]
        try:
            data = await api_get(client, "videos", {
                "part": "statistics",
                "id": ",".join(batch),
            })
            for v in data.get("items", []):
                s = v.get("statistics", {})
                refreshed[v["id"]] = {
                    "views":    int(s.get("viewCount", 0)),
                    "likes":    int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                }
            batches_done += 1
            await asyncio.sleep(0.2)
        except QuotaExceededError as e:
            print(f"  ⚠️ {e} — останавливаем refresh")
            break
        except Exception as e:
            print(f"  ⚠️ Refresh batch failed: {e} — продолжаем")

    print(f"  ✓ Refreshed {len(refreshed)} videos ({batches_done} батчей, ~{batches_done} units)")
    return refreshed


def apply_refreshed_stats(items: list, refreshed: dict) -> list:
    """Применяем обновлённую статистику и пересчитываем производные метрики."""
    now = datetime.now(timezone.utc)
    updated = 0
    for item in items:
        vid_id = item.get("id")
        if vid_id not in refreshed:
            continue
        stats = refreshed[vid_id]
        old_views = item.get("views", 0)
        item["views"]    = stats["views"]
        item["likes"]    = stats["likes"]
        item["comments"] = stats["comments"]
        item["velocity"] = calc_velocity(stats["views"], item.get("published_at", ""))
        item["stats_updated_at"] = now.isoformat()  # обновляем метку последнего обновления статистики

        # Пересчитываем hot_score
        if stats["views"] > 0:
            try:
                age_h = max(1, (now - datetime.fromisoformat(
                    item["published_at"].replace("Z", "+00:00")
                )).total_seconds() / 3600)
                engagement = stats["likes"] + stats["comments"] * 3
                item["hot_score"] = round(engagement / stats["views"] / (1 + age_h / 24), 4)
            except Exception:
                pass

        if old_views != stats["views"]:
            updated += 1

    if updated:
        print(f"  ✓ Stats changed for {updated} videos")
    return items


def save(items: list):
    now = datetime.now(timezone.utc).isoformat()
    existing = {v["id"]: v for v in load_existing()}
    fresh = {v["id"]: v for v in items}

    # Для видео уже существующих в базе — сохраняем неизменяемые поля
    for vid_id, item in fresh.items():
        if vid_id in existing:
            prev = existing[vid_id]
            # added_at — дата первого появления на сайте, никогда не меняется
            item["added_at"] = prev.get("added_at") or now
            # first_seen — когда впервые попало в выборку трендов, никогда не меняется
            item["first_seen"] = prev.get("first_seen") or prev.get("trending_since") or now
        else:
            item["added_at"] = now
            item["first_seen"] = item.get("first_seen") or now

    merged_all = {**existing, **fresh}
    pruned = prune_old(list(merged_all.values()))
    merged = {i['id']: i for i in pruned}
    sorted_items = sorted(merged.values(), key=lambda x: x.get("hot_score") or 0, reverse=True)
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(sorted_items),
        "items": sorted_items,
    }
    atomic_write(OUTPUT_FILE, output)
    size = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"✓ Saved {len(sorted_items)} items → {OUTPUT_FILE} ({size:.0f} KB)")


async def main():
    print(f"YouTube scraper starting at {datetime.now(timezone.utc).isoformat()}")
    quota_log = load_quota_log()
    try:
        async with httpx.AsyncClient(timeout=30) as refresh_client:
            # 1. Собираем новые видео
            items = await scrape_youtube()

            # 2. Обновляем статистику уже существующих видео в базе
            existing = load_existing()
            existing_ids = {v["id"] for v in items}  # свежие уже обновлены
            to_refresh = [v for v in existing if v["id"] not in existing_ids]

            if to_refresh:
                refreshed = await refresh_existing_stats(refresh_client, to_refresh)
                if refreshed:
                    to_refresh = apply_refreshed_stats(to_refresh, refreshed)

            # Объединяем: свежие + обновлённые старые
            all_items = items + to_refresh

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        all_items = []
    finally:
        save_quota_log(quota_log, quota_used)
        print_quota()

    if all_items:
        try:
            save(all_items)
        except Exception as e:
            print(f"❌ Ошибка сохранения: {e}")
    else:
        print("No items to save")


if __name__ == "__main__":
    asyncio.run(main())
