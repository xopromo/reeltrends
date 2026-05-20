import asyncpg
import os
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/reeltrends")


async def get_connection():
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    conn = await get_connection()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT,
            platform TEXT NOT NULL,
            followers_count INT DEFAULT 0,
            median_views FLOAT DEFAULT 0,
            niche TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS reels (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            author_id TEXT REFERENCES authors(id),
            caption TEXT,
            thumbnail_url TEXT,
            published_at TIMESTAMP,
            views INT DEFAULT 0,
            likes INT DEFAULT 0,
            comments INT DEFAULT 0,
            x_factor FLOAT,
            velocity FLOAT,
            hot_score FLOAT,
            niche TEXT,
            trending_since TIMESTAMP,
            frame_count INT DEFAULT 0,
            is_parsed BOOLEAN DEFAULT FALSE,
            scraped_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_reels_hot_score ON reels(hot_score DESC);
        CREATE INDEX IF NOT EXISTS idx_reels_x_factor ON reels(x_factor DESC);
        CREATE INDEX IF NOT EXISTS idx_reels_trending_since ON reels(trending_since DESC);
        CREATE INDEX IF NOT EXISTS idx_reels_niche ON reels(niche);
    """)
    await conn.close()
    print("✓ Database initialized")


async def upsert_author(conn, author: dict):
    await conn.execute("""
        INSERT INTO authors (id, username, display_name, platform, followers_count, median_views, niche, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (id) DO UPDATE SET
            followers_count = $5,
            median_views = $6,
            updated_at = NOW()
    """,
        author["profile_id"],
        author["username"],
        author.get("display_name", ""),
        author["platform"],
        author.get("followers_count", 0),
        author.get("median_views", 0),
        author.get("niche"),
    )


async def upsert_reel(conn, reel: dict):
    author = reel["author"]
    await upsert_author(conn, author)

    await conn.execute("""
        INSERT INTO reels (
            id, url, author_id, caption, thumbnail_url, published_at,
            views, likes, comments, x_factor, velocity, hot_score,
            niche, trending_since, frame_count, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,NOW())
        ON CONFLICT (id) DO UPDATE SET
            views = $7, likes = $8, comments = $9,
            x_factor = $10, velocity = $11, hot_score = $12,
            trending_since = COALESCE(reels.trending_since, $14),
            updated_at = NOW()
    """,
        reel["id"],
        reel["url"],
        author["profile_id"],
        reel.get("caption", ""),
        reel.get("thumbnail_url"),
        datetime.fromisoformat(reel["published_at"].replace("Z", "")) if reel.get("published_at") else None,
        reel.get("views", 0),
        reel.get("likes", 0),
        reel.get("comments", 0),
        reel.get("x_factor"),
        reel.get("velocity"),
        reel.get("hot_score"),
        reel.get("niche") or author.get("niche"),
        datetime.fromisoformat(reel["trending_since"].replace("Z", "")) if reel.get("trending_since") else None,
        reel.get("frame_count", 0),
    )


async def get_trends(
    sort: str = "hot_score",
    niche: str = "",
    platform: str = "",
    days: int = None,
    page: int = 1,
    per_page: int = 20,
    q: str = "",
):
    conn = await get_connection()

    sort_map = {
        "hot_score": "r.hot_score DESC",
        "x_factor": "r.x_factor DESC",
        "views": "r.views DESC",
        "recent": "r.trending_since DESC",
    }
    order = sort_map.get(sort, "r.hot_score DESC")

    conditions = ["r.x_factor IS NOT NULL"]
    params = []
    i = 1

    if niche:
        niches = [n.strip() for n in niche.split(",") if n.strip()]
        placeholders = ",".join(f"${j}" for j in range(i, i + len(niches)))
        conditions.append(f"r.niche IN ({placeholders})")
        params.extend(niches)
        i += len(niches)

    if platform:
        conditions.append(f"a.platform = ${i}")
        params.append(platform)
        i += 1

    if days:
        conditions.append(f"r.published_at > NOW() - INTERVAL '{days} days'")

    if q:
        conditions.append(f"(r.caption ILIKE ${i} OR a.username ILIKE ${i})")
        params.append(f"%{q}%")
        i += 1

    where = " AND ".join(conditions)
    offset = (page - 1) * per_page

    rows = await conn.fetch(f"""
        SELECT
            r.*,
            a.username, a.display_name, a.platform as author_platform,
            a.followers_count, a.median_views as author_median_views, a.niche as author_niche
        FROM reels r
        JOIN authors a ON r.author_id = a.id
        WHERE {where}
        ORDER BY {order}
        LIMIT {per_page} OFFSET {offset}
    """, *params)

    total = await conn.fetchval(f"""
        SELECT COUNT(*) FROM reels r
        JOIN authors a ON r.author_id = a.id
        WHERE {where}
    """, *params)

    await conn.close()
    return [dict(r) for r in rows], total


async def get_niches():
    conn = await get_connection()
    rows = await conn.fetch("""
        SELECT niche, COUNT(*) as cnt
        FROM reels
        WHERE niche IS NOT NULL
        GROUP BY niche
        ORDER BY cnt DESC
    """)
    await conn.close()
    return [dict(r) for r in rows]
