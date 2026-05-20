"""
Scraper — собирает тренды с piratex.ai и сохраняет в PostgreSQL.
Каждая сессия = новый анонимный пользователь = 1 страница бесплатно.
"""

import asyncio
import httpx
import logging
from db import init_db, upsert_reel, get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://piratex.ai"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://piratex.ai/trends",
    "Origin": "https://piratex.ai",
}

SORTS = ["hot_score", "x_factor", "views", "recent"]
PLATFORMS = ["", "instagram", "youtube", "tiktok"]


async def fetch_one_page(page: int, sort: str = "hot_score", platform: str = "", niche: str = "") -> list:
    """Одна сессия = одна страница (лимит анонима)."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as session:
        # Инициализируем сессию — получаем анонимную куку
        try:
            await session.get(f"{BASE_URL}/api/auth/me")
        except Exception as e:
            log.warning(f"Auth init failed: {e}")
            return []

        # Запрашиваем страницу трендов
        try:
            r = await session.get(f"{BASE_URL}/api/trends", params={
                "tab": "global",
                "niche": niche,
                "platform": platform,
                "sort": sort,
                "page": page,
                "per_page": 20,
            })
            r.raise_for_status()
            data = r.json()
            items = data.get("items", [])
            total = data.get("total", 0)
            log.info(f"  page={page} sort={sort} platform={platform or 'all'}: {len(items)} items (total={total})")
            return items
        except Exception as e:
            log.error(f"  page={page} failed: {e}")
            return []


async def scrape_all(max_pages: int = 31) -> int:
    """Собирает все доступные тренды по всем сортировкам."""
    await init_db()
    conn = await get_connection()

    seen_ids = set()
    total_saved = 0

    for sort in SORTS:
        log.info(f"▶ Scraping sort={sort}")
        for page in range(1, max_pages):
            items = await fetch_one_page(page=page, sort=sort)
            if not items:
                break

            new_items = [item for item in items if item["id"] not in seen_ids]
            for item in new_items:
                try:
                    await upsert_reel(conn, item)
                    seen_ids.add(item["id"])
                    total_saved += 1
                except Exception as e:
                    log.error(f"  upsert failed for {item.get('id')}: {e}")

            log.info(f"  +{len(new_items)} new (total saved: {total_saved})")

            # Пауза чтобы не нагружать сервер
            await asyncio.sleep(1.5)

        # Пауза между сортировками
        await asyncio.sleep(3)

    await conn.close()
    log.info(f"✓ Done. Total unique reels saved: {total_saved}")
    return total_saved


async def scrape_incremental() -> int:
    """Быстрый инкрементальный сбор — только первые страницы (новые тренды)."""
    await init_db()
    conn = await get_connection()

    seen_ids = set()
    total_saved = 0

    # Только первые 5 страниц по горячему и x_factor
    for sort in ["hot_score", "x_factor", "recent"]:
        log.info(f"▶ Incremental sort={sort}")
        for page in range(1, 6):
            items = await fetch_one_page(page=page, sort=sort)
            if not items:
                break

            new_items = [item for item in items if item["id"] not in seen_ids]
            for item in new_items:
                try:
                    await upsert_reel(conn, item)
                    seen_ids.add(item["id"])
                    total_saved += 1
                except Exception as e:
                    log.error(f"  upsert failed: {e}")

            await asyncio.sleep(1)

    await conn.close()
    log.info(f"✓ Incremental done. Saved: {total_saved}")
    return total_saved


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "incremental"
    if mode == "full":
        asyncio.run(scrape_all())
    else:
        asyncio.run(scrape_incremental())
