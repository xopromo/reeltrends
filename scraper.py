"""
Scraper — собирает тренды с piratex.ai.
Сохраняет trends.json с метаданными.
"""

import asyncio
import httpx
import json
import os
from datetime import datetime, timezone

BASE_URL = "https://piratex.ai"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://piratex.ai/trends",
    "Origin": "https://piratex.ai",
}

OUTPUT_FILE = "trends.json"
MAX_PAGES = 2  # тест — 2 страницы = 40 рилсов. Для полного сбора поменяй на 31


async def fetch_page(page: int, sort: str = "hot_score") -> list:
    """Каждая сессия = новый аноним = 1 страница бесплатно."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as s:
        try:
            await s.get(f"{BASE_URL}/api/auth/me")
        except Exception as e:
            print(f"  auth failed p{page}: {e}")
            return []
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
            await asyncio.sleep(1.2)
        await asyncio.sleep(2)
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


def save(items: dict):
    sorted_items = sorted(
        items.values(),
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

    size_mb = os.path.getsize(OUTPUT_FILE) / 1024 / 1024
    print(f"✓ Saved {len(sorted_items)} items → {OUTPUT_FILE} ({size_mb:.1f} MB)")


async def main():
    print(f"Starting at {datetime.now(timezone.utc).isoformat()}")
    existing = load_existing()
    print(f"  Existing: {len(existing)} items")
    fresh = await scrape()
    merged = {**existing, **fresh}
    print(f"  Merged: {len(merged)} items")
    save(merged)


if __name__ == "__main__":
    asyncio.run(main())
