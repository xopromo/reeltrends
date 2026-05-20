"""
Scraper — собирает тренды с piratex.ai.
Сохраняет trends.json с метаданными обновления.
"""

import asyncio
import httpx
import json
import os
import random
from datetime import datetime, timezone

BASE_URL = "https://piratex.ai"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Samsung Galaxy S23) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

OUTPUT_FILE = "trends.json"
MAX_PAGES = 200


def random_headers() -> dict:
    ua = random.choice(USER_AGENTS)
    is_mobile = "Mobile" in ua or "iPhone" in ua or "Android" in ua
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": random.choice([
            "ru-RU,ru;q=0.9,en;q=0.8",
            "en-US,en;q=0.9",
            "ru,en-US;q=0.9,en;q=0.8",
            "en-GB,en;q=0.9,ru;q=0.8",
        ]),
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://piratex.ai/trends",
        "Origin": "https://piratex.ai",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if not is_mobile:
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = random.choice(['"Windows"', '"macOS"'])
    return headers


async def fetch_page(page: int, sort: str = "hot_score") -> list:
    headers = random_headers()
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as s:
        try:
            await s.get(f"{BASE_URL}/api/auth/me")
        except Exception as e:
            print(f"  auth failed p{page}: {e}")
            return []
        await asyncio.sleep(random.uniform(0.5, 1.5))
        try:
            r = await s.get(f"{BASE_URL}/api/trends", params={
                "tab": "global", "niche": "", "platform": "",
                "sort": sort, "page": page, "per_page": 20,
            })
            r.raise_for_status()
            items = r.json().get("items", [])
            print(f"  page={page} sort={sort}: {len(items)} items")
            return items
        except Exception as e:
            print(f"  page={page} failed: {e}")
            return []


async def scrape() -> dict:
    seen = {}
    for sort in ["hot_score", "x_factor", "views", "recent"]:
        print(f"▶ sort={sort}")
        for page in range(1, MAX_PAGES):
            items = await fetch_page(page=page, sort=sort)
            if not items:
                break
            for item in items:
                if item["id"] not in seen:
                    seen[item["id"]] = item
            await asyncio.sleep(random.uniform(0.8, 2.5))
        await asyncio.sleep(random.uniform(1.5, 4.0))
    print(f"✓ Total unique: {len(seen)}")
    return seen


def load_existing() -> dict:
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE) as f:
                data = json.load(f)
                return {item["id"]: item for item in data.get("items", [])}
        except Exception:
            pass
    return {}


def save(items: dict, prev_ids: set):
    now = datetime.now(timezone.utc)
    sorted_items = sorted(
        items.values(),
        key=lambda x: x.get("hot_score") or 0,
        reverse=True
    )

    # Считаем новые за последние 24 часа
    from datetime import timedelta
    cutoff_24h = now - timedelta(hours=24)
    new_ids = set(items.keys()) - prev_ids
    new_last_24h = [
        item for item in sorted_items
        if item["id"] in new_ids
        and item.get("trending_since")
        and datetime.fromisoformat(
            item["trending_since"].replace("Z", "+00:00")
        ) > cutoff_24h
    ]

    output = {
        "updated_at": now.isoformat(),
        "total": len(sorted_items),
        "new_count": len(new_ids),           # новых с прошлого запуска
        "new_last_24h": len(new_last_24h),   # новых за 24 часа
        "items": sorted_items,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    size_mb = os.path.getsize(OUTPUT_FILE) / 1024 / 1024
    print(f"✓ Saved {len(sorted_items)} items ({len(new_ids)} new) → {OUTPUT_FILE} ({size_mb:.1f} MB)")
    print(f"  New in last 24h: {len(new_last_24h)}")


async def main():
    print(f"Starting at {datetime.now(timezone.utc).isoformat()}")
    existing = load_existing()
    prev_ids = set(existing.keys())
    print(f"  Existing: {len(existing)} items")
    fresh = await scrape()
    merged = {**existing, **fresh}
    print(f"  Merged: {len(merged)} items")
    save(merged, prev_ids)


if __name__ == "__main__":
    asyncio.run(main())
