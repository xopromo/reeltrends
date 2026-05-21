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


async def api_get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    params["key"] = API_KEY
    r = await client.get(f"{BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


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


def save_channels(channels: dict):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)
    print(f"  ✓ Channels saved: {len(channels)}")


def update_channel_score(channels: dict, cid: str, shorts_found: list):
    """Обновляем рейтинг канала на основе его Shorts."""
    if cid not in channels:
        return
    ch = channels[cid]

    if not shorts_found:
        # Канал не дал видео — небольшой штраф
        ch["score"] = round(ch.get("score", 0) - 0.5, 1)
        return

    # Считаем средний x_factor
    xfactors = [s.get("x_factor") or 0 for s in shorts_found]
    avg_xf = sum(xfactors) / len(xfactors) if xfactors else 0
    viral = [s for s in shorts_found if (s.get("x_factor") or 0) >= 5]

    # Обновляем счётчики
    ch["viral_count"] = ch.get("viral_count", 0) + len(viral)
    ch["last_seen"] = datetime.now(timezone.utc).isoformat()

    # Скор: +2 за каждый вирусный Short, +0.5 за обычный, -0.2 за пустой
    delta = len(viral) * 2 + (len(shorts_found) - len(viral)) * 0.5
    ch["score"] = round(ch.get("score", 0) + delta, 1)


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
    """Ищет каналы похожие на вирусные видео."""
    added = 0
    now = datetime.now(timezone.utc).isoformat()

    for vid in viral_video_ids[:10]:  # не более 10 запросов
        try:
            data = await api_get(client, "search", {
                "part": "snippet",
                "type": "channel",
                "relatedToVideoId": vid,
                "maxResults": 5,
            })
            for item in data.get("items", []):
                cid = item["id"].get("channelId")
                if cid and cid not in channels:
                    channels[cid] = {
                        "id": cid,
                        "title": item["snippet"]["channelTitle"],
                        "lang": "unknown",
                        "niche": "related",
                        "score": 1.0,  # небольшой бонус — найден через вирус
                        "viral_count": 0,
                        "last_seen": None,
                        "added_at": now,
                    }
                    added += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"  Related search failed for {vid}: {e}")

    print(f"  Discovered {added} related channels from viral videos")
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
        except Exception as e:
            print(f"  Channel stats failed: {e}")
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
        except Exception as e:
            print(f"  Video details failed: {e}")
    return results


async def scrape_youtube() -> list:
    if not API_KEY:
        print("❌ YOUTUBE_API_KEY не задан!")
        return []

    async with httpx.AsyncClient(timeout=30) as client:

        # 1. Загружаем базу каналов
        channels = load_channels()
        run_count = sum(1 for ch in channels.values() if ch.get("last_seen"))
        print(f"✓ Loaded {len(channels)} channels (run #{run_count})")

        # 2. Поиск новых каналов
        # Ночной режим — полный переобход всех запросов
        # Обычный режим — каждые 7 запусков
        if NIGHT_RUN:
            print("🌙 Ночной дообор — расширенный поиск каналов...")
            added = await discover_new_channels(client, channels, SEARCH_QUERIES)
            added += await discover_channels_via_shorts(client, channels, SEARCH_QUERIES)
            channels = prune_channels(channels)
            save_channels(channels)
        elif MANUAL_RUN or run_count % 3 == 0 or len(channels) < 100:
            print("🔍 Discovering new channels from search queries...")
            added = await discover_new_channels(client, channels, SEARCH_QUERIES)
            added += await discover_channels_via_shorts(client, channels, SEARCH_QUERIES)
            channels = prune_channels(channels)
            save_channels(channels)

        channel_ids = list(channels.keys())

        # 3. Статистика каналов
        print(f"📊 Getting channel stats for {len(channel_ids)} channels...")
        chan_stats = await get_channel_stats(client, channel_ids)

        # 4. Свежие видео с каналов
        # Ночной режим — берём больше видео с каждого канала
        videos_per_channel = 10 if NIGHT_RUN else 5
        cutoff_days = 14 if NIGHT_RUN else 7
        print(f"📥 Fetching recent videos ({'ночной режим: ' + str(videos_per_channel) + ' видео/канал' if NIGHT_RUN else 'обычный режим'})...")
        all_video_ids = []
        video_to_channel = {}

        cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
        for cid in channel_ids:
            playlist_id = chan_stats.get(cid, {}).get("uploads_playlist", "")
            recent = await get_recent_videos(client, playlist_id, max_results=videos_per_channel, days=cutoff_days)
            for v in recent:
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

            # Фильтр языка
            audio_lang = snippet.get("defaultAudioLanguage", "")[:2]
            if audio_lang and audio_lang not in ("ru", "en"):
                continue

            views = int(stats.get("viewCount", 0))
            if views < 1000:
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
            niche = channels.get(cid, {}).get("niche", "other")
            lang = channels.get(cid, {}).get("lang", "en")

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
                "trending_since": datetime.now(timezone.utc).isoformat(),
            }
            shorts.append(item)

            if cid not in channel_shorts:
                channel_shorts[cid] = []
            channel_shorts[cid].append(item)

        # 7. Считаем x_factor и hot_score
        for short in shorts:
            cid = short["author"]["channel_id"]
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
            update_channel_score(channels, cid, channel_shorts.get(cid, []))

        # 9. Сохраняем обновлённую базу каналов
        save_channels(channels)

        # Статистика базы
        good = sum(1 for ch in channels.values() if ch.get("score", 0) > 2)
        viral_ch = sum(1 for ch in channels.values() if ch.get("viral_count", 0) > 0)
        print(f"  Channel stats: {len(channels)} total, {good} good (score>2), {viral_ch} ever viral")

        shorts.sort(key=lambda x: x.get("hot_score") or 0, reverse=True)
        return shorts


def get_ttl_days(item: dict) -> float:
    xf    = item.get("x_factor") or 0
    views = item.get("views") or 0
    vel   = item.get("velocity") or 0
    if xf > 10 and views > 500000: return float("inf")
    if vel > 100 and views > 100000: return float("inf")
    if xf > 5  and views > 100000: return 365
    if xf > 2  and views > 10000:  return 90
    return 30


def prune_old(items: list) -> list:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    kept, removed = [], 0
    for item in items:
        ttl = get_ttl_days(item)
        if ttl == float("inf"):
            kept.append(item)
            continue
        pub_str = item.get("published_at") or item.get("trending_since")
        if not pub_str:
            kept.append(item)
            continue
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            if (now - pub).total_seconds() / 86400 < ttl:
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


def save(items: list):
    existing = {v["id"]: v for v in load_existing()}
    fresh = {v["id"]: v for v in items}
    merged_all = {**existing, **fresh}
    pruned = prune_old(list(merged_all.values()))
    merged = {i['id']: i for i in pruned}
    sorted_items = sorted(merged.values(), key=lambda x: x.get("hot_score") or 0, reverse=True)
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
