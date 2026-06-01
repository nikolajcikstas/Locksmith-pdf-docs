from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from PIL import Image


SYSTEM_INSTRUCTIONS = """You review image crops for an automotive locksmith documentation app.
Approve only annotated instructional visuals that explain a locksmith action or verification step: pin/wafer
position maps, cut-position diagrams, door/trunk/ignition references, lock-cylinder/procedure diagrams, or
part-location diagrams where the visual relationship matters. Reject plain photos of keys, blades, remotes,
or fobs when they only identify the product and do not explain what to do. Also reject blank crops, section
headers, page footers, pure text, tables, decorative photos, logos, and repeated watermark-only regions.
If the useful information is mostly text/table, reject it because the app renders that as HTML text, not an
image. Return strict JSON only."""

SCHEMA_INSTRUCTIONS = """You transcribe an automotive locksmith instructional diagram into structured data.
The source image is for internal analysis only and must not be copied or described stylistically.
Extract only operational facts visible in an annotated diagram: panel/lock labels, grid cell text or marks,
position numbers, and short operational notes. The full page may contain surrounding tables and prose; ignore
them unless they label the operational visual. Do not guess missing labels or values. Reject plain key/fob
photos, product photos, text-only tables, or illegible visuals. Return strict JSON only."""


def image_review_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("ASSET_AI_REVIEW", "1") != "0"


def review_image_crop(image: Image.Image, context: str, local_section: str) -> dict[str, Any] | None:
    if not image_review_enabled():
        return None

    api_key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("OPENAI_ASSET_MODEL", os.environ.get("OPENAI_OCR_MODEL", "gpt-4o-mini"))
    buffer = BytesIO()
    preview = image.convert("RGB")
    if max(preview.size) > 900:
        preview.thumbnail((900, 900))
    preview.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    prompt = {
        "task": "Decide whether this crop should be shown to paying locksmith users.",
        "local_suggested_section": local_section,
        "page_context": context[:1600],
        "return_json_schema": {
            "publish": "boolean",
            "section": "one of: key_remote, mechanical_key, making_key, programming",
            "caption": "short user-facing caption",
            "reason": "short reason",
        },
    }
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)},
                    {"type": "input_image", "image_url": data_url},
                ],
            },
        ],
        "temperature": 0,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=80) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    text = extract_response_text(data)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def extract_diagram_structure(image: Image.Image, context: str) -> dict[str, Any] | None:
    if not image_review_enabled():
        return None

    api_key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("OPENAI_ASSET_MODEL", os.environ.get("OPENAI_OCR_MODEL", "gpt-4o-mini"))
    buffer = BytesIO()
    preview = image.convert("RGB")
    if max(preview.size) > 1200:
        preview.thumbnail((1200, 1200))
    preview.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    prompt = {
        "task": "Extract structured facts for a newly drawn in-app diagram. Never return source pixels.",
        "page_context": context[:1800],
        "return_json_schema": {
            "publish": "boolean",
            "title": "short diagram purpose",
            "placement": "one of: mechanical_key, making_key, programming",
            "panels": [
                {
                    "label": "lock or component label",
                    "columns": ["short column label"],
                    "rows": [["short visible cell text or filled marker"]],
                    "note": "short visible operational note or empty string",
                }
            ],
            "caption": "short explanation of how this helps the locksmith",
            "reason": "short confidence/rejection reason",
        },
    }
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": SCHEMA_INSTRUCTIONS},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)},
                    {"type": "input_image", "image_url": data_url},
                ],
            },
        ],
        "temperature": 0,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    text = extract_response_text(data)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not bool(parsed.get("publish")):
        return None
    panels = parsed.get("panels")
    if not isinstance(panels, list) or not panels:
        return None
    return parsed


def extract_response_text(data: dict[str, Any]) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    clean = "\n".join(chunks).strip()
    return clean or None
