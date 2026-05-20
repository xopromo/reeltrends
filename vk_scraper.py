"""
VK Scraper — два режима:

РАЗВЕДЧИК (--scout, user token):
  - video.search по ключевым словам → новые видео
  - groups.search по нишам → новые группы
  - Сохраняет группы в vk_groups.json

РАБОТЯГА (--worker, service token):
  - video.get для каждой группы из vk_groups.json
  - Фильтрует короткие вертикальные видео
  - Считает метрики, сохраняет в vk.json
"""

import asyncio
import httpx
import json
import os
import sys
import statistics
from datetime import datetime, timezone, timedelta

SERVICE_TOKEN = os.environ.get("VK_SERVICE_TOKEN", "")
USER_TOKEN    = os.environ.get("VK_USER_TOKEN", "")
MODE          = os.environ.get("VK_MODE", "worker")  # scout | worker

BASE      = "https://api.vk.com/method"
VERSION   = "5.199"
GROUPS_FILE = "vk_groups.json"
OUTPUT_FILE = "vk.json"
MAX_GROUPS  = 500

# Поисковые запросы для разведчика
SCOUT_QUERIES = [
    # Русские ниши
    "фитнес", "недвижимость", "юрист", "психология",
    "бизнес", "финансы", "маркетинг", "саморазвитие",
    "авто", "кулинария", "юмор", "путешествия",
    "здоровье", "мотивация", "лайфхак", "стройка",
    # Английские
    "fitness", "motivation", "funny", "life hack",
    "business", "finance", "travel", "cooking",
]


async def api(client: httpx.AsyncClient, method: str, params: dict) -> dict:
    token = USER_TOKEN if MODE == "scout" else SERVICE_TOKEN
    params["access_token"] = token
    params["v"] = VERSION
    r = await client.get(f"{BASE}/{method}", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise Exception(f"VK API error {data['error']['error_code']}: {data['error']['error_msg']}")
    return data.get("response", {})


def load_groups() -> dict:
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_groups(groups: dict):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    print(f"  ✓ Groups saved: {len(groups)}")


def is_short_vertical(item: dict) -> bool:
    """Короткое (≤60с) вертикальное видео."""
    dur = item.get("duration", 0)
    w   = item.get("width", 1)
    h   = item.get("height", 1)
    return dur <= 60 and h > w


def best_thumb(item: dict) -> str:
    """Лучший thumbnail из списка image."""
    images = item.get("image", [])
    best = ""
    best_w = 0
    for img in images:
        if img.get("width", 0) > best_w and "sun" in img.get("url", ""):
            best_w = img["width"]
            best = img["url"]
    if not best and images:
        best = images[-1].get("url", "")
    return best


def calc_velocity(views: int, date_ts: int) -> float:
    if not date_ts:
        return 0.0
    hours = max(0.5, (datetime.now(timezone.utc).timestamp() - date_ts) / 3600)
    return round(views / hours, 1)


def to_item(v: dict, group_id: int, niche: str) -> dict:
    views    = v.get("views", 0) or v.get("local_views", 0)
    likes    = v.get("likes", {}).get("count", 0)
    reposts  = v.get("reposts", {}).get("count", 0)
    comments = v.get("comments", 0)
    date_ts  = v.get("date", 0)
    pub_iso  = datetime.fromtimestamp(date_ts, tz=timezone.utc).isoformat() if date_ts else ""
    vid      = f"-{abs(group_id)}_{v['id']}"
    url      = v.get("direct_url") or v.get("share_url") or f"https://vkvideo.ru/video{vid}"

    velocity     = calc_velocity(views, date_ts)
    repost_rate  = round(reposts / views, 4) if views > 0 else 0
    hot_score    = round((likes + reposts * 3 + comments * 2) / max(views, 1), 4)

    return {
        "id":           vid,
        "url":          url,
        "title":        v.get("title", ""),
        "caption":      v.get("description", "")[:300],
        "thumbnail_url": best_thumb(v),
        "published_at": pub_iso,
        "duration_sec": v.get("duration", 0),
        "views":        views,
        "likes":        likes,
        "reposts":      reposts,
        "comments":     comments,
        "velocity":     velocity,
        "repost_rate":  repost_rate,
        "x_factor":     None,
        "hot_score":    hot_score,
        "platform":     "vk",
        "niche":        niche,
        "lang":         "ru",
        "author": {
            "channel_id":      str(group_id),
            "username":        str(abs(group_id)),
            "display_name":    str(abs(group_id)),
            "platform":        "vk",
            "followers_count": 0,
            "median_views":    0,
        },
        "trending_since": datetime.now(timezone.utc).isoformat(),
    }


# ── РАЗВЕДЧИК ──────────────────────────────────────────────────────

async def scout():
    """Ищет новые группы через user token."""
    if not USER_TOKEN:
        print("❌ VK_USER_TOKEN не задан!")
        return

    groups = load_groups()
    print(f"✓ Loaded {len(groups)} existing groups")
    added  = 0
    now    = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=20) as client:
        for query in SCOUT_QUERIES:
            print(f"  🔍 groups.search: {query}")
            try:
                resp = await api(client, "groups.search", {
                    "q": query, "type": "group,page", "count": 20,
                    "sort": 6,  # по числу участников
                })
                for g in resp.get("items", []):
                    gid = str(-g["id"])
                    if gid not in groups:
                        groups[gid] = {
                            "id":       -g["id"],
                            "title":    g.get("name", ""),
                            "niche":    query,
                            "score":    0.0,
                            "added_at": now,
                        }
                        added += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    ⚠ {e}")

        # Поиск видео → извлекаем группы из результатов
        for query in SCOUT_QUERIES[:10]:
            print(f"  🎬 video.search: {query}")
            try:
                resp = await api(client, "video.search", {
                    "q": query, "count": 50, "filters": "vk",
                    "shorter": 60, "sort": 2,
                })
                for v in resp.get("items", []):
                    owner = v.get("owner_id", 0)
                    if owner < 0:  # группа
                        gid = str(owner)
                        if gid not in groups:
                            groups[gid] = {
                                "id":       owner,
                                "title":    "",
                                "niche":    query,
                                "score":    1.0,  # бонус — найдена через видео
                                "added_at": now,
                            }
                            added += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"    ⚠ {e}")

    # Прунинг если больше MAX_GROUPS
    if len(groups) > MAX_GROUPS:
        sorted_g = sorted(groups.values(), key=lambda x: x.get("score", 0), reverse=True)
        groups   = {str(g["id"]): g for g in sorted_g[:MAX_GROUPS]}
        print(f"  Pruned to {MAX_GROUPS} groups")

    save_groups(groups)
    print(f"✓ Scout done. Added {added} new groups. Total: {len(groups)}")


# ── РАБОТЯГА ───────────────────────────────────────────────────────

async def worker():
    """Мониторит группы из vk_groups.json через service token."""
    if not SERVICE_TOKEN:
        print("❌ VK_SERVICE_TOKEN не задан!")
        return

    groups = load_groups()
    if not groups:
        print("⚠ No groups found. Run scout first.")
        return

    print(f"✓ Monitoring {len(groups)} groups")
    cutoff  = datetime.now(timezone.utc) - timedelta(days=7)
    all_items: list[dict] = []
    group_views: dict[str, list] = {}

    async with httpx.AsyncClient(timeout=20) as client:
        for gid_str, ginfo in groups.items():
            gid   = ginfo["id"]
            niche = ginfo.get("niche", "other")
            try:
                resp = await api(client, "video.get", {
                    "owner_id": gid,
                    "count":    10,
                    "extended": 0,
                })
                items = resp.get("items", [])
                for v in items:
                    # Фильтр: короткое вертикальное + свежее
                    if not is_short_vertical(v):
                        continue
                    date_ts = v.get("date", 0)
                    if date_ts:
                        pub = datetime.fromtimestamp(date_ts, tz=timezone.utc)
                        if pub < cutoff:
                            continue
                    views = v.get("views", 0) or v.get("local_views", 0)
                    if views < 500:
                        continue
                    item = to_item(v, gid, niche)
                    all_items.append(item)
                    if gid_str not in group_views:
                        group_views[gid_str] = []
                    group_views[gid_str].append(views)

                    # Обновляем score группы
                    reposts = v.get("reposts", {}).get("count", 0)
                    if reposts > 100 or views > 10000:
                        groups[gid_str]["score"] = round(
                            groups[gid_str].get("score", 0) + 2, 1
                        )
                await asyncio.sleep(0.15)
            except Exception as e:
                print(f"  ⚠ group {gid}: {e}")
                groups[gid_str]["score"] = round(
                    groups[gid_str].get("score", 0) - 0.5, 1
                )

    # Считаем x_factor
    for item in all_items:
        gid_str = item["author"]["channel_id"]
        views_list = group_views.get(gid_str, [item["views"]])
        median = statistics.median(views_list) if views_list else item["views"]
        item["author"]["median_views"] = round(median)
        if median > 0:
            item["x_factor"] = round(item["views"] / median, 2)

    all_items.sort(key=lambda x: x.get("hot_score") or 0, reverse=True)
    print(f"✓ Found {len(all_items)} short vertical videos")

    save_groups(groups)
    save_output(all_items)


def save_output(items: list):
    # Мерж с существующими
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE) as f:
                existing = {i["id"]: i for i in json.load(f).get("items", [])}
        except Exception:
            pass
    fresh = {i["id"]: i for i in items}
    merged = sorted(
        {**existing, **fresh}.values(),
        key=lambda x: x.get("hot_score") or 0,
        reverse=True
    )
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total":      len(merged),
        "items":      merged,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    size = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"✓ Saved {len(merged)} items → {OUTPUT_FILE} ({size:.0f} KB)")


async def main():
    print(f"VK Scraper [{MODE}] starting at {datetime.now(timezone.utc).isoformat()}")
    if MODE == "scout":
        await scout()
    else:
        await worker()


if __name__ == "__main__":
    asyncio.run(main())
