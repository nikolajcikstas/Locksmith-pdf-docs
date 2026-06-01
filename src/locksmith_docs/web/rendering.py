from __future__ import annotations

import html
from urllib.parse import quote_plus


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def youtube_embed(video_id: str) -> str:
    safe_id = esc(video_id)
    return (
        '<div class="video-frame">'
        f'<iframe src="https://www.youtube-nocookie.com/embed/{safe_id}" '
        'title="YouTube video player" frameborder="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
        'allowfullscreen loading="lazy"></iframe></div>'
    )


def youtube_search_embed(query: str) -> str:
    safe_query = esc(quote_plus(query))
    return (
        '<div class="video-frame">'
        f'<iframe src="https://www.youtube-nocookie.com/embed?listType=search&list={safe_query}" '
        'title="YouTube training videos" frameborder="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
        'allowfullscreen loading="lazy"></iframe></div>'
    )


def render_video_card(video: dict) -> str:
    video_id = video.get("youtube_video_id") or ""
    query = video.get("search_query") or video.get("title") or "automotive locksmith training"
    if is_search_video_id(video_id):
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        media = youtube_search_embed(query)
    else:
        search_url = f"https://www.youtube.com/watch?v={video_id}"
        media = youtube_embed(video_id)
    return f"""
    <article class="video-card">
      {media}
      <div class="video-copy">
        <h3>{esc(video["title"])}</h3>
        <p>{esc(video.get("description") or "")}</p>
        <span>{esc(video.get("make") or "All makes")} {esc(video.get("model") or "")}</span>
        <a href="{esc(search_url)}" target="_blank" rel="noopener">Open on YouTube</a>
      </div>
    </article>
    """


def is_search_video_id(video_id: str) -> bool:
    return video_id == "dQw4w9WgXcQ" or video_id.startswith("SEARCH_")
