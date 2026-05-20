"""
FastAPI бэкенд + планировщик сбора данных каждые 30 минут.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import get_niches, get_trends, init_db
from scraper import scrape_incremental

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def scheduled_scrape():
    log.info("⏰ Scheduled scrape started")
    try:
        n = await scrape_incremental()
        log.info(f"⏰ Scheduled scrape done: {n} reels")
    except Exception as e:
        log.error(f"⏰ Scheduled scrape failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Первый сбор при старте
    asyncio.create_task(scrape_incremental())
    # Планировщик каждые 30 минут
    scheduler.add_job(scheduled_scrape, "interval", minutes=30)
    scheduler.start()
    log.info("✓ Scheduler started (every 30 min)")
    yield
    scheduler.shutdown()


app = FastAPI(title="ReelTrends", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/trends")
async def api_trends(
    sort: str = Query("hot_score", enum=["hot_score", "x_factor", "views", "recent"]),
    niche: str = Query(""),
    platform: str = Query(""),
    days: int = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, le=100),
    q: str = Query(""),
):
    items, total = await get_trends(
        sort=sort, niche=niche, platform=platform,
        days=days, page=page, per_page=per_page, q=q,
    )

    def fmt(row: dict) -> dict:
        return {
            "id": row["id"],
            "url": row["url"],
            "caption": row["caption"],
            "thumbnail_url": row["thumbnail_url"],
            "published_at": row["published_at"].isoformat() if row["published_at"] else None,
            "views": row["views"],
            "likes": row["likes"],
            "comments": row["comments"],
            "x_factor": row["x_factor"],
            "velocity": row["velocity"],
            "hot_score": row["hot_score"],
            "niche": row["niche"],
            "trending_since": row["trending_since"].isoformat() if row["trending_since"] else None,
            "author": {
                "username": row["username"],
                "display_name": row["display_name"],
                "platform": row["author_platform"],
                "followers_count": row["followers_count"],
                "median_views": row["author_median_views"],
                "niche": row["author_niche"],
            },
        }

    return {
        "items": [fmt(r) for r in items],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@app.get("/api/niches")
async def api_niches():
    return await get_niches()


@app.get("/api/status")
async def api_status():
    from db import get_connection
    conn = await get_connection()
    total = await conn.fetchval("SELECT COUNT(*) FROM reels")
    last = await conn.fetchval("SELECT MAX(updated_at) FROM reels")
    await conn.close()
    return {
        "total_reels": total,
        "last_updated": last.isoformat() if last else None,
        "next_scrape": scheduler.get_jobs()[0].next_run_time.isoformat() if scheduler.get_jobs() else None,
        "now": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/scrape")
async def api_scrape_now():
    """Ручной запуск сбора данных."""
    asyncio.create_task(scrape_incremental())
    return {"status": "started"}


# Отдаём фронтенд
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
