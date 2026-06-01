from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen


SYSTEM_INSTRUCTIONS = """You clean OCR text from automotive locksmith reference pages.
Keep exact factual identifiers unchanged: years, vehicle models, part numbers, FCC IDs, frequencies,
keyways, code series, spacing/depth values, tool names, chip names, PIN notes and decoder references.
Fix OCR substitutions and broken words. Remove copyright/footer/watermark text. Do not invent facts.
Return clean plain text only."""


def ai_cleanup_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("OCR_AI_CLEANUP", "1") != "0"


def clean_page_with_ai(text: str, model: str | None = None) -> str | None:
    if not ai_cleanup_enabled() or not text.strip():
        return None

    api_key = os.environ["OPENAI_API_KEY"]
    model = model or os.environ.get("OPENAI_OCR_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": text[:12000]},
        ],
        "temperature": 0,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    return extract_response_text(data)


def extract_response_text(data: dict) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    clean = "\n".join(chunks).strip()
    return clean or None
