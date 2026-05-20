"""
YouTube Shorts scraper.
1. Ищет каналы по ключевым словам (русские + глобальные)
2. Мониторит их новые Shorts
3. Считает x_factor и velocity
4. Сохраняет в youtube.json
"""

import asyncio
import httpx
import json
import os
import re
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
BASE = "https://www.googleapis.com/youtube/v3"
OUTPUT_FILE = "youtube.json"

# ─── Стартовые поисковые запросы ──────────────────────────────────
SEARCH_QUERIES = [
    # Русскоязычные ниши
    {"q": "фитнес shorts", "lang": "ru", "region": "RU"},
    {"q": "недвижимость shorts", "lang": "ru", "region": "RU"},
    {"q": "юрист shorts", "lang": "ru", "region": "RU"},
    {"q": "маркетинг shorts", "lang": "ru", "region": "RU"},
    {"q": "психология shorts", "lang": "ru", "region": "RU"},
    {"q": "бизнес shorts", "lang": "ru", "region": "RU"},
    {"q": "рецепты shorts", "lang": "ru", "region": "RU"},
    {"q": "финансы shorts", "lang": "ru", "region": "RU"},
    {"q": "путешествия shorts", "lang": "ru", "region": "RU"},
    {"q": "юмор пранк shorts", "lang": "ru", "region": "RU"},
    # Глобальные ниши
    {"q": "fitness shorts viral", "lang": "en", "region": "US"},
    {"q": "motivation shorts viral", "lang": "en", "region": "US"},
    {"q": "funny shorts viral", "lang": "en", "region": "US"},
    {"q": "life hack shorts viral", "lang": "en", "region": "US"},
    {"q": "tech shorts viral", "lang": "en", "region": "US"},
]

# Уже известные каналы (накапливаются автоматически)
CHANNELS_FILE = "yt_channels.json"


def parse_duration(iso: str) -> int:
    """PT1M30S → 90 секунд."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    return h * 3600 + mins * 60 + secs


def calc_x_factor(views: int, channel_median: float) -> float | None:
    if not channel_median or channel_median <= 0:
        return None
    return round(views / channel_median, 2)


def calc_velocity(views: int, published_at: str) -> float | None:
    try:
        pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        hours = max(0.5, (datetime.now(timezone.utc) - pub).total_seconds() / 3600)
        return round(views / hours, 1)
    except Exception:
        return None


async def api_get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    params["key"] = API_KEY
    r = await client.get(f"{BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


async def discover_channels(client: httpx.AsyncClient) -> dict:
    """Ищет каналы по ключевым словам, возвращает {channel_id: info}."""
    channels = {}

    for query in SEARCH_QUERIES:
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
                    }
            print(f"  Found {len(data.get('items', []))} channels for: {query['q']}")
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"  Search failed for {query['q']}: {e}")

    return channels


async def get_channel_stats(client: httpx.AsyncClient, channel_ids: list) -> dict:
    """Получает статистику каналов для расчёта медианы."""
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
                        .get("relatedPlaylists", {})
                        .get("uploads", ""),
                }
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"  Channel stats failed: {e}")
    return stats


async def get_recent_shorts(client: httpx.AsyncClient, playlist_id: str, max_results: int = 10) -> list:
    """Берёт последние видео из плейлиста загрузок канала."""
    if not playlist_id:
        return []
    try:
        data = await api_get(client, "playlistItems", {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": max_results,
        })
        return [
            {
                "video_id": item["snippet"]["resourceId"]["videoId"],
                "published_at": item["snippet"]["publishedAt"],
            }
            for item in data.get("items", [])
        ]
    except Exception:
        return []


async def get_video_details(client: httpx.AsyncClient, video_ids: list) -> list:
    """Получает детали и статистику видео пачками по 50."""
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
        except Exception as e:
            print(f"  Video details failed: {e}")
    return results


def is_short(duration_iso: str, title: str, description: str) -> bool:
    """Определяет является ли видео Shorts."""
    secs = parse_duration(duration_iso)
    has_tag = "#shorts" in (title + description).lower() or "#short" in (title + description).lower()
    return secs <= 180 and (has_tag or secs <= 60)


async def scrape_youtube() -> list:
    """Основная функция сбора данных."""
    if not API_KEY:
        print("❌ YOUTUBE_API_KEY не задан!")
        return []

    async with httpx.AsyncClient(timeout=30) as client:

        # 1. Загружаем или находим каналы
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE) as f:
                channels = json.load(f)
            print(f"✓ Loaded {len(channels)} channels from cache")
        else:
            print("🔍 Discovering channels...")
            channels = await discover_channels(client)
            with open(CHANNELS_FILE, "w") as f:
                json.dump(channels, f, ensure_ascii=False, indent=2)
            print(f"✓ Discovered {len(channels)} channels")

        channel_ids = list(channels.keys())

        # 2. Получаем статистику каналов (плейлисты загрузок)
        print(f"📊 Getting channel stats...")
        chan_stats = await get_channel_stats(client, channel_ids)

        # 3. Для каждого канала берём последние видео
        all_video_ids = []
        video_to_channel = {}  # video_id → channel_id

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)  # видео за неделю

        print(f"📥 Fetching recent videos from {len(channel_ids)} channels...")
        for cid in channel_ids:
            playlist_id = chan_stats.get(cid, {}).get("uploads_playlist", "")
            recent = await get_recent_shorts(client, playlist_id, max_results=5)

            for v in recent:
                # Фильтруем старые
                try:
                    pub = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
                    if pub < cutoff:
                        continue
                except Exception:
                    continue
                all_video_ids.append(v["video_id"])
                video_to_channel[v["video_id"]] = cid

            await asyncio.sleep(0.1)

        print(f"  Found {len(all_video_ids)} recent videos")

        if not all_video_ids:
            print("  No recent videos found")
            return []

        # 4. Получаем детали видео
        print(f"📈 Getting video details...")
        videos = await get_video_details(client, all_video_ids)

        # 5. Фильтруем только Shorts и считаем метрики
        shorts = []
        channel_views = {}  # для расчёта медианы

        for v in videos:
            details = v.get("contentDetails", {})
            snippet = v.get("snippet", {})
            stats = v.get("statistics", {})

            duration = details.get("duration", "")
            title = snippet.get("title", "")
            desc = snippet.get("description", "")

            if not is_short(duration, title, desc):
                continue

            # Только русский и английский контент
            audio_lang = snippet.get("defaultAudioLanguage", "")[:2]
            if audio_lang and audio_lang not in ("ru", "en"):
                continue

            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            comments = int(stats.get("commentCount", 0))

            # Минимум 1000 просмотров
            if views < 1000:
                continue
            published_at = snippet.get("publishedAt", "")
            cid = video_to_channel.get(v["id"], "")
            channel_title = snippet.get("channelTitle", "")
            subs = chan_stats.get(cid, {}).get("subscribers", 0)

            # Накапливаем просмотры для расчёта медианы канала
            if cid not in channel_views:
                channel_views[cid] = []
            channel_views[cid].append(views)

            velocity = calc_velocity(views, published_at)
            niche = channels.get(cid, {}).get("niche", "other")
            lang = channels.get(cid, {}).get("lang", "en")

            thumb = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumb.get("maxres", {}).get("url") or
                thumb.get("standard", {}).get("url") or
                thumb.get("high", {}).get("url") or ""
            )

            shorts.append({
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
                "x_factor": None,  # посчитаем после медианы
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
                "trending_since": datetime.now(timezone.utc).isoformat(),
            })

        # 6. Считаем медиану и x_factor
        import statistics
        for short in shorts:
            cid = short["author"]["channel_id"]
            views_list = channel_views.get(cid, [short["views"]])
            median = statistics.median(views_list) if views_list else short["views"]
            short["author"]["median_views"] = round(median)
            short["x_factor"] = calc_x_factor(short["views"], median)

            # hot_score: engagement с затуханием
            if short["views"] > 0:
                age_h = max(1, (datetime.now(timezone.utc) -
                    datetime.fromisoformat(short["published_at"].replace("Z", "+00:00"))
                ).total_seconds() / 3600)
                engagement = short["likes"] + short["comments"] * 3
                short["hot_score"] = round(engagement / short["views"] / (1 + age_h / 24), 4)

        # Сортируем по hot_score
        shorts.sort(key=lambda x: x.get("hot_score") or 0, reverse=True)
        print(f"✓ Found {len(shorts)} Shorts")
        return shorts


def load_existing() -> list:
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE) as f:
                return json.load(f).get("items", [])
        except Exception:
            pass
    return []


def save(items: list):
    existing = {v["id"]: v for v in load_existing()}
    fresh = {v["id"]: v for v in items}
    merged = {**existing, **fresh}

    sorted_items = sorted(
        merged.values(),
        key=lambda x: x.get("hot_score") or 0,
        reverse=True
    )

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(sorted_items),
        "items": sorted_items,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    size = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"✓ Saved {len(sorted_items)} items → {OUTPUT_FILE} ({size:.0f} KB)")


async def main():
    print(f"YouTube scraper starting at {datetime.now(timezone.utc).isoformat()}")
    items = await scrape_youtube()
    if items:
        save(items)
    else:
        print("No items to save")


if __name__ == "__main__":
    asyncio.run(main())
