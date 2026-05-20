"""
Scraper — собирает тренды с piratex.ai.
Сохраняет:
  trends.json       — метаданные (лёгкий, ~2 MB)
  thumbnails/       — картинки отдельными файлами
"""

import asyncio
import base64
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
THUMB_DIR = "thumbnails"


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


async def fetch_thumbnail(reel_id: str) -> bytes | None:
    """Скачивает thumbnail через прокси piratex."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as s:
        try:
            await s.get(f"{BASE_URL}/api/auth/me")
            r = await s.get(f"{BASE_URL}/api/trends/thumbnail/{reel_id}")
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                return r.content
        except Exception as e:
            print(f"  thumb failed {reel_id}: {e}")
    return None


async def download_thumbnails(items: dict) -> None:
    """Скачивает картинки которых ещё нет в папке thumbnails/."""
    os.makedirs(THUMB_DIR, exist_ok=True)

    # Только те у которых нет файла
    need = [
        item_id for item_id in items
        if not os.path.exists(f"{THUMB_DIR}/{item_id}.jpg")
    ]

    print(f"  Thumbnails: {len(items)-len(need)} cached, {len(need)} to download")
    if not need:
        return

    downloaded = 0
    BATCH = 5

    for i in range(0, len(need), BATCH):
        batch = need[i:i + BATCH]
        tasks = [fetch_thumbnail(reel_id) for reel_id in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for reel_id, data in zip(batch, results):
            if isinstance(data, bytes) and len(data) > 100:
                path = f"{THUMB_DIR}/{reel_id}.jpg"
                with open(path, "wb") as f:
                    f.write(data)
                downloaded += 1

        if (i // BATCH) % 10 == 0:
            print(f"  {min(i+BATCH, len(need))}/{len(need)} thumbnails...")
        await asyncio.sleep(0.3)

    print(f"  ✓ Downloaded {downloaded} new thumbnails")


async def scrape() -> dict:
    seen = {}
    for sort in ["hot_score", "x_factor", "views", "recent"]:
        print(f"▶ sort={sort}")
        for page in range(1, 31):
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

    await download_thumbnails(merged)
    save(merged)


if __name__ == "__main__":
    asyncio.run(main())
