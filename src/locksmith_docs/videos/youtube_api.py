from __future__ import annotations

import json
import os
from urllib.parse import urlencode
from urllib.request import urlopen

from locksmith_docs.db.repository import LocksmithRepository


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def search_youtube(api_key: str, query: str) -> dict | None:
    params = urlencode(
        {
            "part": "snippet",
            "type": "video",
            "maxResults": "1",
            "videoEmbeddable": "true",
            "safeSearch": "moderate",
            "q": query,
            "key": api_key,
        }
    )
    with urlopen(f"{YOUTUBE_SEARCH_URL}?{params}", timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("items") or []
    return items[0] if items else None


def refresh_blog_videos() -> None:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("YOUTUBE_API_KEY is not set; keeping seeded fallback videos.")
        return

    repo = LocksmithRepository()
    updated = 0
    for video in repo.list_blog_videos(limit=20):
        query = video.get("search_query")
        if not query:
            continue
        result = search_youtube(api_key, query)
        if not result:
            continue
        snippet = result["snippet"]
        repo.update_video_from_youtube(
            video_id=video["id"],
            youtube_video_id=result["id"]["videoId"],
            title=snippet.get("title") or video["title"],
            description=snippet.get("description") or video.get("description") or "",
        )
        updated += 1
    print(f"Updated {updated} YouTube videos.")


if __name__ == "__main__":
    refresh_blog_videos()
