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

# Стартовые группы — работяга начнёт с них до первой разведки
SEED_GROUPS = [
    {"id": -29534144,   "title": "MDK",              "niche": "юмор"},
    {"id": -40316705,   "title": "Лепра",             "niche": "юмор"},
    {"id": -57846937,   "title": "Орёл и решка",      "niche": "путешествия"},
    {"id": -22822305,   "title": "Futured",           "niche": "мотивация"},
    {"id": -47200925,   "title": "Psychology",        "niche": "психология"},
    {"id": -91038986,   "title": "Бизнес молодость",  "niche": "бизнес"},
    {"id": -16108331,   "title": "Эстетика спорта",   "niche": "фитнес"},
    {"id": -128666765,  "title": "Лайфхак",           "niche": "лайфхак"},
    {"id": -42909645,   "title": "Психология жизни",  "niche": "психология"},
    {"id": -34547719,   "title": "Подслушано",        "niche": "юмор"},
    {"id": -66589208,   "title": "Маркетинг и бизнес","niche": "маркетинг"},
    {"id": -63489781,   "title": "Финансы для людей", "niche": "финансы"},
]
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


def atomic_write(path: str, data):
    """Атомарная запись — защита от повреждения при краше."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def get_group_tier(g: dict) -> int:
    """
    Тир по score:
    0 (score>2)  — каждый прогон
    1 (0..2)     — каждый 2й прогон
    2 (<0)       — каждый 4й прогон
    """
    score = g.get("score", 0)
    if score > 2:  return 0
    if score >= 0: return 1
    return 2

TIER_INTERVAL = {0: 1, 1: 2, 2: 4}

def should_check_group(g: dict, run_count: int) -> bool:
    if not g.get("last_seen"):
        return True  # новая группа — всегда проверяем
    tier = get_group_tier(g)
    return run_count % TIER_INTERVAL[tier] == 0


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
    groups = {}
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE) as f:
                groups = json.load(f)
        except Exception:
            pass
    # Добавляем стартовые группы если база пустая
    if not groups:
        now = datetime.now(timezone.utc).isoformat()
        for g in SEED_GROUPS:
            gid = str(g["id"])
            groups[gid] = {**g, "score": 0.0, "added_at": now}
        print(f"  ✓ Loaded {len(groups)} seed groups")
    return groups


def save_groups(groups: dict):
    atomic_write(GROUPS_FILE, groups)
    real = {k:v for k,v in groups.items() if k != "__meta__"}
    print(f"  ✓ Groups saved: {len(real)}")


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
        "first_seen":       datetime.now(timezone.utc).isoformat(),
        "stats_updated_at": datetime.now(timezone.utc).isoformat(),
        "added_at":         None,  # заполняется при merge в save_output()
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

async def refresh_vk_stats(client: httpx.AsyncClient, existing: list) -> dict:
    """
    Обновляем статистику уже существующих видео через video.getById.
    Стоимость: 1 запрос на 200 видео.
    Частота:
    - младше 7 дней  → каждый прогон
    - 7–30 дней      → только ночной/ручной (MODE=scout)
    - старше 30 дней → не обновляем (VK старые видео почти не растут)
    """
    now = datetime.now(timezone.utc)
    to_refresh = []
    for item in existing:
        pub_str = item.get("published_at")
        if not pub_str:
            continue
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            age_days = (now - pub).total_seconds() / 86400
        except Exception:
            continue
        if age_days < 7:
            to_refresh.append(item)
        elif age_days < 30 and MODE == "scout":
            to_refresh.append(item)

    if not to_refresh:
        return {}

    print(f"🔄 Refreshing stats for {len(to_refresh)} existing VK videos...")
    refreshed = {}

    for i in range(0, len(to_refresh), 200):
        batch = to_refresh[i:i+200]
        # VK video ID format: -groupid_videoid
        video_ids = ",".join(item["id"] for item in batch)
        try:
            resp = await api(client, "video.get", {
                "videos": video_ids,
                "count": len(batch),
                "extended": 0,
            })
            for v in resp.get("items", []):
                vid = f"{v.get('owner_id','')}_{v.get('id','')}"
                views   = v.get("views", 0) or v.get("local_views", 0)
                likes   = v.get("likes", {}).get("count", 0)
                reposts = v.get("reposts", {}).get("count", 0)
                comments = v.get("comments", 0)
                refreshed[vid] = {"views": views, "likes": likes, "reposts": reposts, "comments": comments}
            await asyncio.sleep(0.2)
        except Exception as e:
            print(f"  ⚠ Refresh batch failed: {e} — продолжаем")

    # Применяем обновлённую статистику
    updated = 0
    for item in existing:
        vid = item["id"]
        if vid not in refreshed:
            continue
        s = refreshed[vid]
        old_views = item.get("views", 0)
        item["views"]    = s["views"]
        item["likes"]    = s["likes"]
        item["reposts"]  = s["reposts"]
        item["comments"] = s["comments"]
        item["velocity"] = calc_velocity(s["views"], int(
            datetime.fromisoformat(item["published_at"].replace("Z","+00:00")).timestamp()
        ) if item.get("published_at") else 0)
        item["repost_rate"] = round(s["reposts"] / s["views"], 4) if s["views"] > 0 else 0
        item["hot_score"]   = round((s["likes"] + s["reposts"]*3 + s["comments"]*2) / max(s["views"],1), 4)
        item["stats_updated_at"] = now.isoformat()
        if old_views != s["views"]:
            updated += 1

    print(f"  ✓ Refreshed {len(refreshed)} videos, stats changed for {updated}")
    return refreshed


async def worker():
    """Мониторит группы из vk_groups.json через service token."""
    if not SERVICE_TOKEN:
        print("❌ VK_SERVICE_TOKEN не задан!")
        return

    groups = load_groups()
    if not groups:
        print("⚠ No groups found. Run scout first.")
        save_output([])
        return

    # Счётчик прогонов из __meta__
    run_count = groups.get("__meta__", {}).get("run_count", 0) + 1
    if "__meta__" not in groups:
        groups["__meta__"] = {}
    groups["__meta__"]["run_count"] = run_count
    real_groups = {k: v for k, v in groups.items() if k != "__meta__"}

    # Тиринг: фильтруем группы по частоте проверки
    all_gids = list(real_groups.keys())
    check_gids = [gid for gid in all_gids if should_check_group(real_groups[gid], run_count)]
    skipped = len(all_gids) - len(check_gids)
    if skipped:
        print(f"✓ Monitoring {len(check_gids)}/{len(all_gids)} groups (тиринг: пропущено {skipped})")
    else:
        print(f"✓ Monitoring {len(check_gids)} groups")

    now_utc   = datetime.now(timezone.utc)
    all_items: list[dict] = []
    group_views: dict[str, list] = {}

    async with httpx.AsyncClient(timeout=20) as client:
        # Обновляем статистику существующих видео ВНУТРИ клиента
        existing_all = []
        if os.path.exists(OUTPUT_FILE):
            try:
                with open(OUTPUT_FILE) as f:
                    existing_all = json.load(f).get("items", [])
            except Exception:
                pass

        for gid_str in check_gids:
            ginfo = real_groups[gid_str]
            gid   = ginfo["id"]
            niche = ginfo.get("niche", "other")

            # Умное окно: новая группа → 7 дней, известная → 26 часов
            is_new = not ginfo.get("last_seen")
            if is_new:
                cutoff_hours = 7 * 24
                max_count    = 20
            else:
                cutoff_hours = 26
                max_count    = 5   # группы редко дают >3 клипов в сутки

            cutoff_dt = now_utc - timedelta(hours=cutoff_hours)

            try:
                items = []
                for album_id in [-2, None]:
                    params = {"owner_id": gid, "count": max_count, "extended": 0}
                    if album_id is not None:
                        params["album_id"] = album_id
                    try:
                        resp = await api(client, "video.get", params)
                        batch = resp.get("items", [])
                        if batch:
                            items.extend(batch)
                            if album_id == -2:
                                break
                    except Exception:
                        pass

                found_in_group = 0
                for v in items:
                    if not is_short_vertical(v):
                        continue
                    date_ts = v.get("date", 0)
                    if date_ts:
                        pub_dt = datetime.fromtimestamp(date_ts, tz=timezone.utc)
                        if pub_dt < cutoff_dt:
                            continue
                    views = v.get("views", 0) or v.get("local_views", 0)
                    # Свежие видео (< 6ч) не фильтруем по просмотрам
                    age_h = (now_utc.timestamp() - date_ts) / 3600 if date_ts else 999
                    if views < 500 and age_h >= 6:
                        continue
                    item = to_item(v, gid, niche)
                    all_items.append(item)
                    found_in_group += 1
                    if gid_str not in group_views:
                        group_views[gid_str] = []
                    group_views[gid_str].append(views)

                    reposts = v.get("reposts", {}).get("count", 0)
                    if reposts > 100 or views > 10000:
                        real_groups[gid_str]["score"] = round(real_groups[gid_str].get("score", 0) + 2, 1)

                # Обновляем last_seen и накапливаем views_history
                if found_in_group > 0:
                    real_groups[gid_str]["last_seen"] = now_utc.isoformat()
                    vh = real_groups[gid_str].get("views_history", [])
                    vh.extend(group_views.get(gid_str, []))
                    real_groups[gid_str]["views_history"] = vh[-50:]
                    if real_groups[gid_str]["views_history"]:
                        real_groups[gid_str]["median_views"] = round(
                            statistics.median(real_groups[gid_str]["views_history"])
                        )
                else:
                    real_groups[gid_str]["score"] = round(real_groups[gid_str].get("score", 0) - 0.3, 1)

                await asyncio.sleep(0.15)

            except Exception as e:
                print(f"  ⚠ group {gid}: {e} — пропускаем")
                real_groups[gid_str]["score"] = round(real_groups[gid_str].get("score", 0) - 0.5, 1)

    # x_factor из исторической медианы группы
    for item in all_items:
        gid_str = item["author"]["channel_id"]
        g_data  = real_groups.get(gid_str, {})
        hist_median = g_data.get("median_views", 0)
        if hist_median > 0:
            median = hist_median
        else:
            views_list = group_views.get(gid_str, [item["views"]])
            median = statistics.median(views_list) if views_list else item["views"]
        item["author"]["median_views"] = round(median)
        if median > 0:
            item["x_factor"] = round(item["views"] / median, 2)

    all_items.sort(key=lambda x: x.get("hot_score") or 0, reverse=True)
    print(f"✓ Found {len(all_items)} short vertical videos")

    # Обновляем статистику существующих видео (внутри async with выше)
    fresh_ids = {item["id"] for item in all_items}
    to_refresh = [v for v in existing_all if v["id"] not in fresh_ids]

    # Сохраняем группы с __meta__
    groups = {**real_groups, "__meta__": groups["__meta__"]}
    save_groups(groups)
    save_output(all_items + to_refresh)


def get_ttl_days(item: dict) -> float:
    xf    = item.get("x_factor") or 0
    views = item.get("views") or 0
    vel   = item.get("velocity") or 0
    rep   = item.get("repost_rate") or 0
    if xf > 10 and views > 500_000: return float("inf")
    if vel > 100 and views > 100_000: return float("inf")
    if rep > 0.01 and views > 50_000: return float("inf")
    if xf > 5  and views > 100_000: return 365
    if xf > 2  and views > 10_000:  return 90
    if xf > 1  and views > 5_000:   return 60   # промежуточный уровень
    return 30


def prune_old(items: list) -> list:
    now = datetime.now(timezone.utc)
    kept, removed = [], 0
    for item in items:
        ttl = get_ttl_days(item)
        if ttl == float("inf"):
            kept.append(item)
            continue
        # TTL от stats_updated_at — активно растущие видео не вылетают
        ref_str = item.get("stats_updated_at") or item.get("first_seen") or item.get("added_at") or item.get("published_at")
        if not ref_str:
            kept.append(item)
            continue
        try:
            ref = datetime.fromisoformat(ref_str.replace("Z", "+00:00"))
            if (now - ref).total_seconds() / 86400 < ttl:
                kept.append(item)
            else:
                removed += 1
        except Exception:
            kept.append(item)
    if removed:
        print(f"  Pruned {removed} old items (kept {len(kept)})")
    return kept


def save_output(items: list):
    now = datetime.now(timezone.utc).isoformat()
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE) as f:
                existing = {i["id"]: i for i in json.load(f).get("items", [])}
        except Exception:
            pass
    fresh = {i["id"]: i for i in items}

    # Сохраняем неизменяемые поля для уже известных видео
    for vid_id, item in fresh.items():
        if vid_id in existing:
            prev = existing[vid_id]
            item["added_at"]   = prev.get("added_at") or now
            item["first_seen"] = prev.get("first_seen") or prev.get("trending_since") or now
        else:
            item["added_at"]   = now
            item["first_seen"] = item.get("first_seen") or now

    all_items = list({**existing, **fresh}.values())
    all_items = prune_old(all_items)
    merged = sorted(all_items, key=lambda x: x.get("hot_score") or 0, reverse=True)
    output = {
        "updated_at": now,
        "total":      len(merged),
        "items":      merged,
    }
    atomic_write(OUTPUT_FILE, output)
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
