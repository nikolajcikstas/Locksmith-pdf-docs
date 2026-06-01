from __future__ import annotations

import json
from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.connection import get_connection


def load_blog_seed(path: Path | None = None) -> None:
    settings = get_settings()
    seed_path = path or settings.data_dir / "blog_seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))

    with get_connection() as conn:
        conn.execute("DELETE FROM videos")
        for post in seed.get("posts", []):
            conn.execute(
                """
                INSERT INTO blog_posts (slug, title, summary, body_md, status, published_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (slug)
                DO UPDATE SET title = EXCLUDED.title,
                              summary = EXCLUDED.summary,
                              body_md = EXCLUDED.body_md,
                              status = EXCLUDED.status,
                              published_at = EXCLUDED.published_at
                """,
                (
                    post["slug"],
                    post["title"],
                    post.get("summary"),
                    post["body_md"],
                    post.get("status", "draft"),
                ),
            )
        for video in seed.get("videos", []):
            conn.execute(
                """
                INSERT INTO videos
                  (title, youtube_video_id, description, search_query, make, model, year_from, year_to, system_code, tags, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (search_query)
                WHERE search_query IS NOT NULL
                DO UPDATE SET title = EXCLUDED.title,
                              youtube_video_id = EXCLUDED.youtube_video_id,
                              description = EXCLUDED.description,
                              search_query = EXCLUDED.search_query,
                              year_from = EXCLUDED.year_from,
                              year_to = EXCLUDED.year_to,
                              tags = EXCLUDED.tags,
                              status = EXCLUDED.status
                """,
                (
                    video["title"],
                    video["youtube_video_id"],
                    video.get("description"),
                    video.get("search_query"),
                    video.get("make"),
                    video.get("model"),
                    video.get("year_from"),
                    video.get("year_to"),
                    video.get("system_code"),
                    video.get("tags", []),
                    video.get("status", "draft"),
                ),
            )
        conn.commit()


if __name__ == "__main__":
    load_blog_seed()
