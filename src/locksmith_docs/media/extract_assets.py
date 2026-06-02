from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageChops

from locksmith_docs.media.ai_image_reviewer import extract_diagram_structure, image_review_enabled
from locksmith_docs.parsing.section_parser import find_section_code


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "imported_pages.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "asset_candidates.json"
DEFAULT_SCHEMA_CACHE = PROJECT_ROOT / "data" / "ai_diagram_cache.json"
DEFAULT_REPORTS_INPUT = PROJECT_ROOT / "data" / "report_drafts.json"
STATIC_ASSET_DIR = PROJECT_ROOT / "static" / "assets" / "candidates"


def load_pages(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    docs = payload.get("documents", []) if isinstance(payload, dict) else payload
    pages: list[dict[str, Any]] = []
    for doc in docs:
        for page in doc.get("pages", []):
            item = dict(page)
            item["document_name"] = doc.get("name")
            pages.append(item)
    return pages


def image_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    return visual_region_boxes(image)


def visual_region_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    try:
        return cv2_region_boxes(image)
    except Exception:
        return numpy_region_boxes(image)


def cv2_region_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    import cv2

    rgb = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    dark = gray < 190
    saturated = (hsv[:, :, 1] > 45) & (hsv[:, :, 2] < 238)
    mask = (dark | saturated).astype("uint8") * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=1)
    mask = cv2.dilate(mask, np.ones((11, 11), np.uint8), iterations=1)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area < 900:
            continue
        boxes.append(expand_box((int(x), int(y), int(x + w), int(y + h)), image.size, 8))
    return merge_nearby_boxes(boxes, image.size)


def numpy_region_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    rgb = image.convert("RGB")
    scale = max(1, max(rgb.size) // 700)
    if scale > 1:
        rgb = rgb.resize((rgb.width // scale, rgb.height // scale))
    arr = np.array(rgb)
    gray = np.array(rgb.convert("L"))
    color_spread = arr.max(axis=2) - arr.min(axis=2)
    mask = (gray < 185) | ((color_spread > 45) & (gray < 235))
    for _ in range(2):
        padded = np.pad(mask, 1, constant_values=False)
        mask = (
            padded[1:-1, 1:-1]
            | padded[:-2, 1:-1]
            | padded[2:, 1:-1]
            | padded[1:-1, :-2]
            | padded[1:-1, 2:]
        )
    boxes = connected_component_boxes(mask)
    scaled = [(x1 * scale, y1 * scale, x2 * scale, y2 * scale) for x1, y1, x2, y2 in boxes]
    return merge_nearby_boxes(scaled, image.size)


def connected_component_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    boxes = []
    ys, xs = np.where(mask)
    for start_x, start_y in zip(xs.tolist(), ys.tolist()):
        if visited[start_y, start_x] or not mask[start_y, start_x]:
            continue
        stack = [(start_x, start_y)]
        visited[start_y, start_x] = True
        min_x = max_x = start_x
        min_y = max_y = start_y
        count = 0
        while stack:
            x, y = stack.pop()
            count += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nx < 0 or ny < 0 or nx >= width or ny >= height:
                    continue
                if visited[ny, nx] or not mask[ny, nx]:
                    continue
                visited[ny, nx] = True
                stack.append((nx, ny))
        if count >= 80:
            boxes.append((min_x, min_y, max_x + 1, max_y + 1))
    return boxes


def density_bands(values: np.ndarray, threshold: float, min_len: int, gap: int) -> list[tuple[int, int]]:
    bands = []
    start: int | None = None
    last = 0
    for index, value in enumerate(values):
        if value >= threshold:
            if start is None:
                start = index
            last = index
        elif start is not None and index - last > gap:
            if last - start + 1 >= min_len:
                bands.append((start, last + 1))
            start = None
    if start is not None and last - start + 1 >= min_len:
        bands.append((start, last + 1))
    return bands


def merge_nearby_boxes(boxes: list[tuple[int, int, int, int]], size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda item: (item[1], item[0])):
        x1, y1, x2, y2 = expand_box(box, size, 12)
        for index, existing in enumerate(merged):
            if boxes_touch((x1, y1, x2, y2), existing, padding=28):
                ex1, ey1, ex2, ey2 = existing
                merged[index] = (min(x1, ex1), min(y1, ey1), max(x2, ex2), max(y2, ey2))
                break
        else:
            merged.append((x1, y1, x2, y2))
    return [box for box in merged if is_reasonable_crop(box, size)]


def boxes_touch(a: tuple[int, int, int, int], b: tuple[int, int, int, int], padding: int) -> bool:
    return not (a[2] + padding < b[0] or b[2] + padding < a[0] or a[3] + padding < b[1] or b[3] + padding < a[1])


def expand_box(box: tuple[int, int, int, int], size: tuple[int, int], pad: int) -> tuple[int, int, int, int]:
    width, height = size
    x1, y1, x2, y2 = box
    return max(0, x1 - pad), max(0, y1 - pad), min(width, x2 + pad), min(height, y2 + pad)


def is_reasonable_crop(box: tuple[int, int, int, int], size: tuple[int, int]) -> bool:
    width, height = size
    x1, y1, x2, y2 = box
    crop_w = x2 - x1
    crop_h = y2 - y1
    area = crop_w * crop_h
    page_area = width * height
    return 4_500 < area < page_area * 0.35 and crop_w > 80 and crop_h > 55


def classify_crop(crop: Image.Image, page_text: str) -> tuple[str, bool, bool, float]:
    width, height = crop.size
    ratio = width / max(1, height)
    gray = crop.convert("L")
    ink = ImageChops.invert(gray).point(lambda value: 255 if value > 35 else 0)
    bbox = ink.getbbox()
    ink_density = 0.0
    if bbox:
        ink_density = float((np.array(ink) > 0).sum()) / (width * height)

    context = page_text.lower()
    useful_context = any(token in context for token in ("key", "fob", "remote", "blade", "lock", "door", "trunk", "ignition", "lishi"))
    table_context = any(token in context for token in ("spacing", "depth", "macs", "code series", "programmer", "diagnostic"))
    has_text = ink_density > 0.31 and table_context
    key_like = useful_context and 0.006 < ink_density < 0.31 and (ratio > 1.10 or ratio < 0.85 or width > 140 or height > 140)
    annotated = has_visual_instruction_cues(crop, "", page_text)
    kind = "procedure_diagram" if annotated else "key_image" if key_like else "table" if has_text else "unknown"
    explains_step = annotated
    score = min(0.95, 0.58 + (0.25 if annotated else 0) + min(0.12, abs(ratio - 1.0) / 8))
    return kind, has_text, explains_step, score


def build_asset_candidates(
    pages: list[dict[str, Any]],
    limit_pages: int | None = None,
    cache_path: Path = DEFAULT_SCHEMA_CACHE,
    allowed_system_codes: set[str] | None = None,
    progress: Callable[[int, int, int], None] | None = None,
    max_new_ai_requests: int = 5,
) -> list[dict[str, Any]]:
    candidates = []
    pages_to_scan = pages[:limit_pages]
    cache = load_schema_cache(cache_path) if image_review_enabled() else {}
    unsaved_cache_entries = 0
    new_ai_requests = 0
    active_system_by_document: dict[str, str] = {}
    for page_index, page in enumerate(pages_to_scan, start=1):
        image_path = page.get("image_path")
        if not image_path:
            continue
        source = Path(image_path)
        if not source.exists():
            continue
        page_text = page.get("ocr_text") or ""
        document_name = page.get("document_name") or ""
        page_system_code = infer_system_code(page_text, document_name)
        if page_system_code != "UNMATCHED":
            active_system_by_document[document_name] = page_system_code
        system_code = active_system_by_document.get(document_name, page_system_code)
        if not is_valid_system_code(system_code):
            continue
        if allowed_system_codes is not None and system_code not in allowed_system_codes:
            if progress and (page_index % 25 == 0 or page_index == len(pages_to_scan)):
                progress(page_index, len(pages_to_scan), len(candidates))
            continue
        page_upper = page_text.upper()
        if not is_candidate_page(page_upper):
            if progress and (page_index % 25 == 0 or page_index == len(pages_to_scan)):
                progress(page_index, len(pages_to_scan), len(candidates))
            continue
        image = Image.open(source)
        published_on_page = 0
        for index, box in enumerate(diagram_candidate_boxes(image)):
            asset_id = make_asset_id(page, box, index)
            cached_schema = cache.get(asset_id, "__missing__")
            if (
                image_review_enabled()
                and cached_schema == "__missing__"
                and max_new_ai_requests > 0
                and new_ai_requests >= max_new_ai_requests
            ):
                continue
            diagram_candidate = build_original_diagram_candidate(
                page,
                image,
                box,
                page_text,
                system_code,
                index,
                cached_schema=cached_schema,
            )
            if image_review_enabled() and cached_schema == "__missing__":
                new_ai_requests += 1
                cache[asset_id] = diagram_candidate.get("diagram_data") if diagram_candidate else None
                unsaved_cache_entries += 1
                if unsaved_cache_entries >= 3:
                    save_schema_cache(cache_path, cache)
                    unsaved_cache_entries = 0
            if diagram_candidate:
                candidates.append(diagram_candidate)
                published_on_page += 1
                if published_on_page >= 2:
                    break
        if progress and (page_index % 10 == 0 or published_on_page or page_index == len(pages_to_scan)):
            progress(page_index, len(pages_to_scan), len(candidates))
    if image_review_enabled() and unsaved_cache_entries:
        save_schema_cache(cache_path, cache)
    return candidates


def diagram_candidate_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    width, height = image.size
    # Source diagrams can sit beside instructions, above cutting data, or at page bottom.
    # The source page is analyzed internally, while only reconstructed SVG facts are public.
    return [(0, 0, width, height)]


def build_original_diagram_candidate(
    page: dict[str, Any],
    image: Image.Image,
    box: tuple[int, int, int, int],
    page_text: str,
    system_code: str,
    index: int,
    cached_schema: object = "__missing__",
) -> dict[str, Any] | None:
    source_page = image.crop(box)
    if source_page.width < 280 or source_page.height < 100:
        return None
    schema = extract_diagram_structure(source_page, page_text) if cached_schema == "__missing__" else cached_schema
    if not schema or not has_usable_diagram_schema(schema):
        return None
    return build_schema_diagram_candidate(
        schema,
        system_code,
        page.get("document_name"),
        page.get("page_number"),
        make_asset_id(page, box, index),
        list(box),
    )


def build_schema_diagram_candidate(
    schema: dict[str, Any],
    system_code: str,
    source_document: str | None,
    source_page: int | None,
    asset_id: str,
    crop_box: list[int] | None = None,
) -> dict[str, Any] | None:
    schema = normalize_diagram_schema(schema)
    if not schema or not has_usable_diagram_schema(schema):
        return None
    placement = normalize_placement(str(schema.get("placement") or "making_key"))
    caption = clean_caption(str(schema.get("caption") or ""), placement)
    out_path = STATIC_ASSET_DIR / f"{asset_id}.svg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_original_diagram_svg(schema), encoding="utf-8")
    return {
        "id": asset_id,
        "system_code": system_code,
        "title": clean_svg_text(str(schema.get("title") or "Lock-position diagram")),
        "kind": "procedure_image",
        "public_path": f"/static/assets/candidates/{asset_id}.svg",
        "source_document": source_document,
        "source_page": source_page,
        "crop_box": crop_box or [],
        "extracted_text": "",
        "diagram_data": schema,
        "rewritten_caption": caption,
        "placement": placement,
        "usefulness_score": 0.96,
        "watermark_removed": False,
        "visibility": "public",
        "review_status": "approved",
        "decision_reason": "Original diagram rendered from extracted operational facts; no source image is published.",
    }


def build_assets_from_reports(path: Path = DEFAULT_REPORTS_INPUT) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    drafts = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[dict[str, Any]] = []
    for item in drafts if isinstance(drafts, list) else []:
        if not item.get("ai_verified") or item.get("publication_issues"):
            continue
        draft = item.get("draft") or {}
        system_code = str(item.get("system_code") or draft.get("code") or "")
        if not is_valid_system_code(system_code):
            continue
        raw_diagrams = draft.get("procedure_diagrams") or []
        if not isinstance(raw_diagrams, list):
            continue
        diagrams = list(raw_diagrams)
        diagrams.extend(infer_standard_diagrams_from_report(draft, diagrams))
        source_pages = item.get("source_pages") or []
        source_page = source_pages[0] if source_pages else None
        for index, schema in enumerate(diagrams[:3]):
            if not isinstance(schema, dict):
                continue
            asset_id = hashlib.sha1(f"report:{system_code}:{index}:{json.dumps(schema, sort_keys=True)}".encode("utf-8")).hexdigest()[:16]
            candidate = build_schema_diagram_candidate(
                schema,
                system_code,
                item.get("source_document"),
                source_page,
                asset_id,
            )
            if candidate:
                candidates.append(candidate)
    return candidates


def infer_standard_diagrams_from_report(
    draft: dict[str, Any],
    existing_diagrams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create standard SVG requests only when verified technical facts support them."""
    if any(isinstance(diagram, dict) and diagram.get("visual_type") == "blade_orientation" for diagram in existing_diagrams):
        return []
    mechanical = draft.get("mechanical_key") if isinstance(draft.get("mechanical_key"), dict) else {}
    setup = mechanical.get("cutting_setup") if isinstance(mechanical.get("cutting_setup"), dict) else {}
    setup_text = " ".join(str(value) for value in setup.values()).lower()
    milling = str(mechanical.get("milling") or "")
    has_track_facts = (
        "cut track" in setup_text
        or ("left track" in setup_text and "right track" in setup_text)
        or "guide track" in setup_text
    )
    if "internal" not in milling.lower() or not has_track_facts:
        return []
    profile = str(
        mechanical.get("test_key")
        or mechanical.get("ilco_keyway")
        or (draft.get("transponder") or {}).get("test_key")
        or "Mechanical key"
    ).strip()
    if not profile:
        return []
    return [
        {
            "title": f"{profile} Mechanical Blade Orientation",
            "placement": "mechanical_key",
            "caption": f"{profile} orientation reference showing the cutting track and guide track identified in the verified source data.",
            "visual_type": "blade_orientation",
            "blade_profile": profile,
            "milling": milling,
            "cut_track": str(setup.get("cut_track") or "Cut track - apply cut depths here"),
            "guide_track": str(setup.get("guide_track") or "Guide track - clearance only"),
            "note": "Orientation reference only. Use the spacing and depth tables for cutting values.",
        }
    ]


def verified_report_codes(path: Path = DEFAULT_REPORTS_INPUT) -> list[str]:
    if not path.exists():
        return []
    drafts = json.loads(path.read_text(encoding="utf-8"))
    return sorted(
        {
            str(item.get("system_code") or (item.get("draft") or {}).get("code") or "")
            for item in drafts if isinstance(drafts, list)
            if item.get("ai_verified") and not item.get("publication_issues")
            and is_valid_system_code(str(item.get("system_code") or (item.get("draft") or {}).get("code") or ""))
        }
    )


def is_lock_position_diagram_page(page_text: str) -> bool:
    text = page_text.upper()
    lock_labels = sum(
        token in text
        for token in ("IGNITION", "DOOR", "TRUNK", "VALET", "GLOVE BOX", "GLOVE")
    )
    workflow_context = any(
        token in text
        for token in ("CUTTING THE KEY", "MAKING KEY", "METHOD", "LOCK READER", "DECODER", "TUMBLER", "POSITION")
    )
    is_key_making_page = "CUTTING THE KEY" in text or ("METHOD #1" in text and "METHOD #2" in text)
    return workflow_context and (lock_labels >= 2 or is_key_making_page)


def is_valid_system_code(system_code: str) -> bool:
    if system_code in {"", "UNMATCHED"}:
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]{0,12}-[A-Z0-9]{1,5}|[A-Z][A-Z0-9]{1,12}", system_code))


def has_operational_diagram_layout(image: Image.Image) -> bool:
    if not has_grid_or_pointer_geometry(image):
        return False
    arr = np.array(image.convert("L"))
    ink = arr < 210
    height, width = ink.shape
    lower = ink[int(height * 0.43) :, :]
    lower_density = float(lower.sum()) / max(1, lower.size)
    return lower_density > 0.02


def render_original_diagram_svg(schema: dict[str, Any]) -> str:
    if schema.get("visual_type") == "blade_orientation":
        return render_blade_orientation_svg(schema)
    source_panels = schema.get("panels")
    panels = [
        panel for panel in source_panels if isinstance(panel, dict) and panel.get("label")
    ] if isinstance(source_panels, list) else []
    panels = panels[:5]
    if panels and (
        schema.get("key_orientation") == "bow_left_tip_right"
        or is_lock_position_panel(panels[0])
    ):
        return render_integrated_key_position_svg(panels[0])
    margin = 24
    width = 920
    panel_width = width - margin * 2
    draw_key_orientation = schema.get("key_orientation") == "bow_left_tip_right"
    orientation_height = 92 if draw_key_orientation else 0
    panel_heights = [
        76 + orientation_height + len(panel.get("rows") if isinstance(panel.get("rows"), list) else []) * 34
        + (36 if panel.get("note") else 0)
        for panel in panels
    ]
    height = margin + sum(panel_heights) + max(0, len(panels) - 1) * 18 + margin
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect width="100%" height="100%" rx="12" fill="#f7fafb"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#142536} .title{font-size:16px;font-weight:700} .cell{font-size:13px} .note{font-size:12px;fill:#526477} .orientation{font-size:12px;font-weight:700;fill:#526477} .blade{fill:#d7e1e7;stroke:#355067;stroke-width:2} .key-head{fill:#172d3b} .key-hole{fill:#f7fafb} .mark{fill:#116d68}</style>',
    ]
    y_cursor = margin
    for index, panel in enumerate(panels):
        x = margin
        label = clean_svg_text(str(panel.get("label") or ""))[:30]
        source_columns = panel.get("columns")
        columns = [
            clean_svg_text(str(item))[:12] for item in source_columns
        ][:10] if isinstance(source_columns, list) else []
        source_rows = panel.get("rows")
        rows = [
            row for row in source_rows if isinstance(row, list)
        ][:10] if isinstance(source_rows, list) else []
        col_count = max(1, len(columns), max((len(row) for row in rows), default=1))
        has_label_column = bool(columns and columns[0].lower() in {"lock", "location", "component"})
        label_width = min(228, panel_width * 0.28) if has_label_column and col_count > 1 else panel_width / col_count
        value_width = (panel_width - label_width) / max(1, col_count - 1) if has_label_column and col_count > 1 else label_width
        column_widths = [label_width] + [value_width] * (col_count - 1) if has_label_column else [value_width] * col_count
        column_x = [x]
        for width_index in column_widths[:-1]:
            column_x.append(column_x[-1] + width_index)
        parts.append(f'<text class="title" x="{x}" y="{y_cursor + 14}">{xml_escape(label)}</text>')
        if draw_key_orientation and index == 0:
            key_y = y_cursor + 38
            parts.append(f'<text class="orientation" x="{x}" y="{key_y + 16}">Reference orientation</text>')
            parts.append(f'<path class="key-head" d="M {x + 130} {key_y + 6} h 42 a 18 18 0 0 1 18 18 v 32 a 18 18 0 0 1 -18 18 h -42 a 18 18 0 0 1 -18 -18 v -32 a 18 18 0 0 1 18 -18 z"/>')
            parts.append(f'<rect class="key-hole" x="{x + 128}" y="{key_y + 26}" width="18" height="28" rx="8"/>')
            parts.append(f'<path class="blade" d="M {x + 190} {key_y + 26} H {x + 610} l 27 14 -27 14 H {x + 190} Z"/>')
            parts.append(f'<text class="orientation" x="{x + 118}" y="{key_y + 88}">Bow / handle</text>')
            parts.append(f'<text class="orientation" x="{x + 582}" y="{key_y + 88}">Blade tip</text>')
        table_top = y_cursor + 26 + (orientation_height if draw_key_orientation and index == 0 else 0)
        for col, column in enumerate(columns):
            cx = column_x[col]
            col_width = column_widths[col]
            parts.append(f'<rect x="{cx}" y="{table_top}" width="{col_width}" height="26" fill="#e8f1f2" stroke="#c7d8de"/>')
            parts.append(f'<text class="cell" x="{cx + col_width / 2}" y="{table_top + 17}" text-anchor="middle">{xml_escape(column)}</text>')
        for row_index, row in enumerate(rows):
            y = table_top + 26 + row_index * 34
            for col in range(col_count):
                max_chars = 27 if has_label_column and col == 0 else 14
                value = clean_svg_text(str(row[col] if col < len(row) else ""))[:max_chars]
                cx = column_x[col]
                col_width = column_widths[col]
                parts.append(f'<rect x="{cx}" y="{y}" width="{col_width}" height="34" fill="#ffffff" stroke="#c7d8de"/>')
                if value.lower() in {"filled", "mark", "x", "yes", "solid", "square"}:
                    parts.append(f'<rect class="mark" x="{cx + col_width / 2 - 5}" y="{y + 11}" width="10" height="10" rx="2"/>')
                elif has_label_column and col == 0:
                    parts.append(f'<text class="cell" x="{cx + 8}" y="{y + 22}">{xml_escape(value)}</text>')
                else:
                    parts.append(f'<text class="cell" x="{cx + col_width / 2}" y="{y + 22}" text-anchor="middle">{xml_escape(value)}</text>')
        note = clean_svg_text(str(panel.get("note") or ""))[:58]
        if note:
            parts.append(f'<text class="note" x="{x}" y="{table_top + 26 + len(rows) * 34 + 22}">{xml_escape(note)}</text>')
        y_cursor += panel_heights[index] + 18
    parts.append("</svg>")
    return "".join(parts)


def is_lock_position_panel(panel: dict[str, Any]) -> bool:
    rows = panel.get("rows") if isinstance(panel.get("rows"), list) else []
    text = " ".join(
        str(row[0]) for row in rows
        if isinstance(row, list) and row
    ).upper()
    return "IGNITION" in text and any(token in text for token in ("DOOR", "TRUNK", "HATCH"))


def render_integrated_key_position_svg(panel: dict[str, Any]) -> str:
    """Render a redrawn key-shaped position map without publishing source pixels."""
    width = 1120
    height = 360
    rows = [
        row for row in (panel.get("rows") or [])
        if isinstance(row, list) and row
    ][:3]
    note = clean_svg_text(str(panel.get("note") or ""))[:88]
    blade_x = 222
    blade_end = 902
    cell_w = 85
    track_y = [162, 204, 246]
    colors = ["#0f766e", "#2563a6", "#c47a14"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect width="100%" height="100%" rx="20" fill="#f7fafb"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#142536}.title{font-size:21px;font-weight:700}.sub{font-size:13px;fill:#526477}.num{font-size:14px;font-weight:700;fill:#526477}.legend{font-size:13px;font-weight:700}.note{font-size:13px;fill:#526477}.outline{stroke:#264456;stroke-width:2.4}.guide{stroke:#d3dfe4;stroke-width:1}.head{fill:#142d3c}.hole{fill:#f7fafb}</style>',
        '<text class="title" x="34" y="40">HU66 lock-position guide</text>',
        '<text class="sub" x="34" y="62">Internal-milled mechanical key. Read positions from handle to tip.</text>',
        # Compact legend in one visual line.
        f'<circle cx="582" cy="38" r="7" fill="{colors[0]}"/><text class="legend" x="598" y="43">Ignition</text>',
        f'<circle cx="690" cy="38" r="7" fill="{colors[1]}"/><text class="legend" x="706" y="43">Doors / trunk / hatch</text>',
        f'<circle cx="905" cy="38" r="7" fill="{colors[2]}"/><text class="legend" x="921" y="43">Glove box*</text>',
        # Integrated key bow and blade outline.
        '<path class="head" d="M54 133 C54 104 77 82 106 82 H174 C203 82 226 105 226 134 V270 C226 299 203 322 174 322 H106 C77 322 54 300 54 271 Z"/>',
        '<path class="hole" d="M100 149 C100 131 114 117 132 117 H151 C169 117 183 131 183 149 V253 C183 271 169 286 151 286 H132 C114 286 100 272 100 253 Z"/>',
        f'<path fill="#ffffff" class="outline" d="M{blade_x - 8} 134 H{blade_end} L953 202 L{blade_end} 270 H{blade_x - 8} Z"/>',
        f'<line class="guide" x1="{blade_x}" y1="190" x2="{blade_end + 36}" y2="190"/>',
        f'<line class="guide" x1="{blade_x}" y1="232" x2="{blade_end + 36}" y2="232"/>',
    ]
    for index in range(8):
        x = blade_x + index * cell_w
        center = x + cell_w / 2
        parts.append(f'<line class="guide" x1="{x}" y1="134" x2="{x}" y2="270"/>')
        parts.append(f'<text class="num" x="{center}" y="120" text-anchor="middle">{index + 1}</text>')
    parts.append(f'<line class="guide" x1="{blade_end}" y1="134" x2="{blade_end}" y2="270"/>')
    for row_index, row in enumerate(rows):
        y = track_y[row_index]
        for position in range(1, 9):
            value = str(row[position] if position < len(row) else "").strip().lower()
            if value in {"filled", "mark", "x", "yes", "solid", "square"}:
                cx = blade_x + (position - 1) * cell_w + cell_w / 2
                parts.append(f'<rect x="{cx - 11}" y="{y - 11}" width="22" height="22" rx="4" fill="{colors[row_index]}"/>')
    parts.extend([
        '<text class="sub" x="90" y="344">HANDLE</text>',
        '<text class="sub" x="866" y="306">TIP</text>',
        f'<text class="note" x="338" y="316">{xml_escape(note)}</text>' if note else "",
        '<text class="note" x="338" y="338">Filled markers identify the cut positions used by each lock.</text>',
        '</svg>',
    ])
    return "".join(parts)


def render_blade_orientation_svg(schema: dict[str, Any]) -> str:
    """Draw a track-identification reference without inventing a key bitting pattern."""
    profile = clean_svg_text(str(schema.get("blade_profile") or "Mechanical blade"))[:30]
    milling = clean_svg_text(str(schema.get("milling") or "Internal milling"))[:32]
    cut_track = clean_svg_text(str(schema.get("cut_track") or "Left track - cutting track"))[:42]
    guide_track = clean_svg_text(str(schema.get("guide_track") or "Right track - guide only"))[:42]
    note = clean_svg_text(str(schema.get("note") or "Track identification only. This is not a cut-code or bitting pattern."))[:94]
    return "".join([
        '<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="360" viewBox="0 0 1120 360" role="img">',
        '<rect width="100%" height="100%" rx="20" fill="#f7fafb"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#142536}.bo-title{font-size:22px;font-weight:700}.bo-sub{font-size:14px;fill:#526477}.bo-label{font-size:13px;font-weight:700}.bo-track{font-size:13px;fill:#526477}.bo-blade{fill:#e8eef1;stroke:#264456;stroke-width:3}.bo-head{fill:#142d3c}.bo-hole{fill:#f7fafb}.bo-cut{stroke:#0f766e;stroke-width:8;stroke-linecap:round}.bo-guide{stroke:#1d4ed8;stroke-width:7;stroke-dasharray:16 10;stroke-linecap:round}</style>',
        f'<text class="bo-title" x="38" y="46">{xml_escape(profile)} blade orientation</text>',
        f'<text class="bo-sub" x="38" y="71">{xml_escape(milling)} technical reference; handle at left and tip at right.</text>',
        '<line class="bo-cut" x1="458" y1="102" x2="500" y2="102"/>',
        '<text class="bo-label" x="516" y="98">CUT TRACK</text>',
        f'<text class="bo-track" x="516" y="116">{xml_escape(cut_track)}</text>',
        '<line class="bo-guide" x1="790" y1="102" x2="832" y2="102"/>',
        '<text class="bo-label" x="848" y="98">GUIDE TRACK</text>',
        f'<text class="bo-track" x="848" y="116">{xml_escape(guide_track)}</text>',
        '<path class="bo-head" d="M70 171 C70 141 94 118 124 118 H201 C231 118 255 142 255 172 V262 C255 292 231 315 201 315 H124 C94 315 70 292 70 262 Z"/>',
        '<path class="bo-hole" d="M112 180 C112 162 126 148 144 148 H177 C195 148 209 162 209 180 V252 C209 270 195 284 177 284 H144 C126 284 112 270 112 252 Z"/>',
        '<path class="bo-blade" d="M244 176 H865 L945 217 L865 258 H244 Z"/>',
        '<path class="bo-cut" d="M282 200 H844"/>',
        '<path class="bo-guide" d="M282 233 H844"/>',
        '<text class="bo-sub" x="105" y="339">HANDLE</text><text class="bo-sub" x="862" y="282">TIP</text>',
        f'<text class="bo-sub" x="292" y="302">{xml_escape(note)}</text>',
        '<text class="bo-sub" x="292" y="327">Use the spacing and depth tables below when cutting a working blade.</text>',
        '</svg>',
    ])


def has_usable_diagram_schema(schema: dict[str, Any]) -> bool:
    if schema.get("visual_type") == "blade_orientation":
        return bool(str(schema.get("blade_profile") or "").strip() and str(schema.get("cut_track") or "").strip())
    panels = schema.get("panels") or []
    if not isinstance(panels, list):
        return False
    for panel in panels:
        if not isinstance(panel, dict) or not str(panel.get("label") or "").strip():
            continue
        rows = panel.get("rows") or []
        meaningful_rows = [
            row for row in rows
            if isinstance(row, list) and any(str(cell or "").strip() for cell in row)
        ]
        columns = panel.get("columns") or []
        if len(meaningful_rows) >= 2 or (len(meaningful_rows) == 1 and len(columns) >= 2):
            return True
    return False


def normalize_diagram_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(schema)
    panels = []
    source_panels = schema.get("panels")
    for original in source_panels if isinstance(source_panels, list) else []:
        if not isinstance(original, dict):
            continue
        panel = dict(original)
        source_columns = panel.get("columns")
        columns = [
            str(value).strip() for value in source_columns if str(value).strip()
        ] if isinstance(source_columns, list) else []
        source_rows = panel.get("rows")
        rows = source_rows if isinstance(source_rows, list) else []
        if columns and rows and all(not isinstance(row, list) for row in rows):
            rows = [[str(value).strip() for value in rows]]
        else:
            rows = [
                [str(value).strip() for value in row]
                for row in rows if isinstance(row, list)
            ]
        panel["columns"] = columns
        panel["rows"] = rows
        panels.append(panel)
    normalized["panels"] = panels
    return normalized


def clean_svg_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def is_candidate_page(page_upper: str) -> bool:
    if not any(token in page_upper for token in ("KEY", "BLADE", "LOCK", "IGNITION")):
        return False
    lock_labels = sum(token in page_upper for token in ("IGNITION", "DOOR", "DOORS", "TRUNK", "VALET", "GLOVE BOX"))
    pin_map = any(token in page_upper for token in ("PIN", "PINS", "POSITION", "POSITIONS", "WAFER", "WAFERS"))
    cutting_context = any(token in page_upper for token in ("CUTTING THE KEY", "MAKING KEY", "DECODER", "LOCK READER"))
    procedural_context = "CUTTING THE KEY" in page_upper or "MAKING KEY" in page_upper or "METHOD #" in page_upper
    if not (lock_labels >= 2 and (procedural_context or pin_map or cutting_context)):
        return False
    if any(token in page_upper for token in ("TABLE OF CONTENTS", "INTRODUCTION", "ACKNOWLEDGMENTS")):
        return False
    return True


def load_schema_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def save_schema_cache(path: Path, cache: dict[str, Any]) -> None:
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def is_page_header_or_footer(box: tuple[int, int, int, int], size: tuple[int, int]) -> bool:
    _width, height = size
    x1, y1, x2, y2 = box
    crop_h = y2 - y1
    if y1 < 110:
        return True
    if y2 > height - 70 and crop_h < 170:
        return True
    return False


def passes_local_image_quality(image: Image.Image, final: bool = False) -> bool:
    width, height = image.size
    if width < 115 or height < 70:
        return False
    if width * height < 12_000:
        return False
    gray = image.convert("L")
    arr = np.array(gray)
    ink = arr < (238 if final else 245)
    ink_pixels = int(ink.sum())
    density = ink_pixels / max(1, width * height)
    if density < (0.018 if final else 0.012):
        return False
    if density > 0.48:
        return False
    ys, xs = np.where(ink)
    if len(xs) == 0 or len(ys) == 0:
        return False
    content_w = int(xs.max() - xs.min() + 1)
    content_h = int(ys.max() - ys.min() + 1)
    if content_w < 70 or content_h < 45:
        return False
    content_area_ratio = (content_w * content_h) / max(1, width * height)
    if content_area_ratio < 0.08:
        return False
    return True


def has_visual_instruction_cues(image: Image.Image, crop_text: str, page_text: str) -> bool:
    text = f"{crop_text} {page_text[:1200]}".upper()
    cue_words = (
        "PIN",
        "PINS",
        "POSITION",
        "POSITIONS",
        "WAFER",
        "WAFERS",
        "CUT",
        "CUTS",
        "DOOR",
        "DOORS",
        "TRUNK",
        "IGNITION",
        "LOCK",
        "LISHI",
        "DECODER",
        "READER",
        "REMOVE",
        "PROGRAM",
        "CONNECTOR",
        "BYPASS",
    )
    has_text_cue = any(word in text for word in cue_words)
    has_diagram_geometry = has_grid_or_pointer_geometry(image)
    if has_diagram_geometry and has_text_cue:
        return True
    # Allow clear visual procedure diagrams even when OCR misses the labels.
    return has_diagram_geometry and not is_plain_isolated_object(image, crop_text)


def has_grid_or_pointer_geometry(image: Image.Image) -> bool:
    arr = np.array(image.convert("L"))
    ink = arr < 210
    height, width = ink.shape
    if width < 120 or height < 80:
        return False
    row_density = ink.sum(axis=1) / max(1, width)
    col_density = ink.sum(axis=0) / max(1, height)
    horizontal_rules = int((row_density > 0.45).sum())
    vertical_rules = int((col_density > 0.42).sum())
    thin_rule_rows = density_bands(row_density, threshold=0.28, min_len=1, gap=2)
    thin_rule_cols = density_bands(col_density, threshold=0.25, min_len=1, gap=2)
    grid_like = len(thin_rule_rows) >= 2 and len(thin_rule_cols) >= 2
    repeated_small_grid = (
        len(density_bands(row_density, threshold=0.08, min_len=1, gap=2)) >= 5
        and len(density_bands(col_density, threshold=0.08, min_len=1, gap=2)) >= 5
    )
    pointer_like = horizontal_rules >= 1 and vertical_rules >= 1 and width / max(1, height) > 1.2
    return grid_like or repeated_small_grid or pointer_like


def is_plain_isolated_object(image: Image.Image, crop_text: str = "") -> bool:
    if crop_text and any(token in crop_text.upper() for token in ("PIN", "CUT", "DOOR", "TRUNK", "IGNITION", "LOCK")):
        return False
    arr = np.array(image.convert("L"))
    ink = arr < 210
    height, width = ink.shape
    ys, xs = np.where(ink)
    if len(xs) == 0 or len(ys) == 0:
        return True
    content_w = int(xs.max() - xs.min() + 1)
    content_h = int(ys.max() - ys.min() + 1)
    content_ratio = (content_w * content_h) / max(1, width * height)
    if has_grid_or_pointer_geometry(image):
        return False
    return content_ratio > 0.28 and not crop_text.strip()


def looks_like_text_block(image: Image.Image) -> bool:
    width, height = image.size
    if width < 220 or height < 90:
        return False
    arr = np.array(image.convert("L"))
    ink = arr < 225
    density = float(ink.sum()) / max(1, width * height)
    if density > 0.22:
        return False
    row_density = ink.sum(axis=1) / max(1, width)
    bands = density_bands(row_density, threshold=0.012, min_len=2, gap=5)
    if len(bands) < 4:
        return False
    tallest_band = max((end - start for start, end in bands), default=0)
    if tallest_band > 60:
        return False
    return True


def looks_like_section_label(image: Image.Image) -> bool:
    width, height = image.size
    ratio = width / max(1, height)
    if ratio < 2.05 or height > 170:
        return False
    arr = np.array(image.convert("L"))
    ink = arr < 225
    density = float(ink.sum()) / max(1, width * height)
    if density > 0.24:
        return False
    row_density = ink.sum(axis=1) / max(1, width)
    col_density = ink.sum(axis=0) / max(1, height)
    long_horizontal_strokes = int((row_density > 0.35).sum())
    wide_text_or_rule = bool(row_density.max(initial=0) > 0.48)
    sparse_columns = float((col_density > 0.18).sum()) / max(1, width)
    return wide_text_or_rule and long_horizontal_strokes <= 18 and sparse_columns < 0.42


def crop_to_physical_component(image: Image.Image) -> Image.Image | None:
    box = physical_component_box(image)
    if not box:
        return None
    return image.crop(box)


def physical_component_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    arr = np.array(image.convert("L"))
    height, width = arr.shape
    ink = arr < 190
    visited = np.zeros_like(ink, dtype=bool)
    ys, xs = np.where(ink)
    best: tuple[int, int, int, int, int] | None = None
    for start_x, start_y in zip(xs.tolist(), ys.tolist()):
        if visited[start_y, start_x] or not ink[start_y, start_x]:
            continue
        stack = [(start_x, start_y)]
        visited[start_y, start_x] = True
        min_x = max_x = start_x
        min_y = max_y = start_y
        count = 0
        while stack:
            x, y = stack.pop()
            count += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nx < 0 or ny < 0 or nx >= width or ny >= height:
                    continue
                if visited[ny, nx] or not ink[ny, nx]:
                    continue
                visited[ny, nx] = True
                stack.append((nx, ny))
        comp_w = max_x - min_x + 1
        comp_h = max_y - min_y + 1
        if count <= width * height * 0.004:
            continue
        if comp_h <= max(42, height * 0.22) or comp_w <= max(24, width * 0.025):
            continue
        aspect = comp_w / max(1, comp_h)
        if aspect > 1.35:
            continue
        if best is None or count > best[4]:
            pad = 14
            best = (
                max(0, min_x - pad),
                max(0, min_y - pad),
                min(width, max_x + pad),
                min(height, max_y + pad),
                count,
            )
    if best is None:
        return None
    x1, y1, x2, y2, _count = best
    if (x2 - x1) * (y2 - y1) < 8_000:
        return None
    return x1, y1, x2, y2


def crop_ocr_text(image: Image.Image) -> str:
    if os.environ.get("CROP_OCR_REVIEW", "1") == "0":
        return ""
    try:
        import pytesseract
    except Exception:
        return ""
    try:
        gray = image.convert("L")
        text = pytesseract.image_to_string(gray, lang="eng", config="--oem 1 --psm 6")
    except Exception:
        return ""
    return " ".join(text.split())[:800]


def is_text_only_or_header_crop(text: str) -> bool:
    clean = " ".join(text.split())
    if not clean:
        return False
    upper = clean.upper()
    if any(token in upper for token in ("SECTION", "CONTINUED", "AUTOSMART", "COPYRIGHT", "TABLE OF CONTENTS")):
        return True
    alnum = re.sub(r"[^A-Za-z0-9]", "", clean)
    if len(alnum) > 28 and not any(token in upper for token in ("PIN", "CUT", "KEY", "BLADE", "LOCK", "LISHI")):
        return True
    if len(clean.split()) >= 7:
        return True
    return False


def trim_white_border(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    arr = np.array(gray)
    mask = arr < 245
    ys, xs = np.where(mask)
    if len(xs) == 0 or len(ys) == 0:
        return image
    pad = 8
    x1 = max(0, int(xs.min()) - pad)
    y1 = max(0, int(ys.min()) - pad)
    x2 = min(image.width, int(xs.max()) + pad)
    y2 = min(image.height, int(ys.max()) + pad)
    if x2 <= x1 or y2 <= y1:
        return image
    return image.crop((x1, y1, x2, y2))


def infer_asset_placement(page_text: str, crop_text: str = "") -> str:
    text = f"{page_text}\n{crop_text}".lower()
    if any(token in text for token in ("spacing", "depth", "blade", "keyway", "code series", "macs", "cut")):
        return "mechanical_key"
    if any(token in text for token in ("lock", "cylinder", "lishi", "decoder", "reader", "trunk", "door")):
        return "making_key"
    if any(token in text for token in ("program", "pin", "immobilizer", "transponder", "chip")):
        return "programming"
    return "key_remote"


def normalize_placement(value: str) -> str:
    if value in {"key_remote", "mechanical_key", "making_key", "programming"}:
        return value
    return "key_remote"


def build_caption(page_text: str, placement: str = "key_remote") -> str:
    lower = page_text.lower()
    if placement == "mechanical_key":
        return "Mechanical key, blade, or cut reference for this system."
    if placement == "making_key":
        return "Lock, decoder, or cylinder reference for the working-key procedure."
    if placement == "programming":
        return "Programming or transponder reference for the job workflow."
    if "blade" in lower or "emergency" in lower:
        return "Emergency blade reference for field identification."
    if "fob" in lower or "remote" in lower:
        return "Remote or fob reference for field identification."
    if "lock" in lower or "lishi" in lower:
        return "Lock or decoder reference for field work."
    return "Key reference image for field identification."


def clean_caption(value: str, placement: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if 8 <= len(value) <= 140 and not any(token in value.upper() for token in ("SECTION", "CONTINUED", "AUTOSMART", "COPYRIGHT")):
        return value
    return build_caption("", placement)


def infer_system_code(text: str, source_document: str = "") -> str:
    import re

    section_code = find_section_code(text, source_document)
    if section_code:
        return section_code
    match = re.search(r"\b([A-Z]{2,4})[.-]([A-Z0-9]{1,3})\b", text)
    if not match:
        return "UNMATCHED"
    return f"{match.group(1)}-{match.group(2)}"


def make_asset_id(page: dict[str, Any], box: tuple[int, int, int, int], index: int) -> str:
    base = f"{page.get('document_name')}:{page.get('page_number')}:{box}:{index}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def write_manifest(path: Path, candidates: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False), encoding="utf-8")


def load_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else []
    clean_items = []
    for item in items:
        candidate = prepare_manifest_candidate(item)
        if candidate:
            clean_items.append(candidate)
    return clean_items


def prepare_manifest_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    public_path = str(item.get("public_path") or "")
    if item.get("system_code") == "UNMATCHED":
        return None
    if not public_path.startswith("/static/assets/") or not public_path.lower().endswith(".svg"):
        return None
    image_path = PROJECT_ROOT / public_path.lstrip("/")
    if not image_path.exists() or image_path.stat().st_size < 1500:
        return None
    if item.get("decision_reason") != "Original diagram rendered from extracted operational facts; no source image is published.":
        return None
    if not has_usable_diagram_schema(item.get("diagram_data") or {}):
        return None
    candidate = dict(item)
    placement = candidate.get("placement") or infer_asset_placement(str(candidate.get("rewritten_caption") or ""), str(candidate.get("extracted_text") or ""))
    candidate["placement"] = normalize_placement(str(placement))
    candidate["rewritten_caption"] = clean_caption(str(candidate.get("rewritten_caption") or ""), candidate["placement"])
    candidate["kind"] = "procedure_image"
    candidate["visibility"] = "public"
    candidate["review_status"] = "approved"
    return candidate


def import_to_database(
    candidates: list[dict[str, Any]],
    replace: bool = False,
    replace_system_codes: list[str] | None = None,
) -> dict[str, int]:
    from locksmith_docs.db.repository import LocksmithRepository

    repo = LocksmithRepository()
    if replace:
        repo.clear_public_assets()
    elif replace_system_codes:
        repo.clear_public_assets_for_systems(replace_system_codes)
    imported = 0
    skipped = 0
    for candidate in candidates:
        if repo.upsert_asset_candidate(candidate):
            imported += 1
        else:
            skipped += 1
    return {"imported": imported, "skipped": skipped, "total": len(candidates)}


def published_system_codes() -> set[str]:
    from locksmith_docs.db.connection import get_connection

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT code FROM systems WHERE status = 'approved' AND publication_source = 'ai_verified'"
        ).fetchall()
    return {str(row["code"]) for row in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract internal key-image candidates from imported PDF page images.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_SCHEMA_CACHE)
    parser.add_argument("--limit-pages", type=int)
    parser.add_argument(
        "--max-new-ai-requests",
        type=int,
        default=int(os.environ.get("AI_MAX_NEW_DIAGRAMS_PER_RUN", "5")),
        help="Maximum new paid AI page reviews for optional deep diagram recovery; cached pages remain reusable.",
    )
    parser.add_argument("--to-db", action="store_true")
    parser.add_argument("--from-manifest", action="store_true", help="Import existing asset_candidates.json without rescanning PDF pages.")
    parser.add_argument("--from-reports", action="store_true", help="Render diagrams already extracted during report verification without additional AI requests.")
    parser.add_argument("--replace", action="store_true", help="Replace current public assets during import.")
    parser.add_argument("--clean-manifest", action="store_true", help="Rewrite the asset manifest after local quality filtering.")
    parser.add_argument("--job-id", help="Processing job id to update while diagrams are generated.")
    args = parser.parse_args()

    if args.from_reports:
        candidates = build_assets_from_reports()
        write_manifest(args.output, candidates)
    elif args.from_manifest:
        candidates = load_manifest(args.output)
        if args.clean_manifest:
            write_manifest(args.output, candidates)
    else:
        if args.to_db and not image_review_enabled():
            raise RuntimeError(
                "AI diagram generation is not available. Add OPENAI_API_KEY to the Docker .env file, "
                "restart the app, and run rebuild again."
            )
        progress = None
        if args.job_id:
            from locksmith_docs.processing.job_status import update_job

            def progress(done: int, total: int, accepted: int) -> None:
                update_job(
                    args.job_id,
                    "running",
                    f"Building original procedure diagrams: scanned {done}/{total} pages, approved {accepted}.",
                )

        candidates = build_asset_candidates(
            load_pages(args.input),
            args.limit_pages,
            args.cache,
            published_system_codes() if args.to_db else None,
            progress,
            args.max_new_ai_requests,
        )
        if args.to_db and not candidates:
            raise RuntimeError(
                "No original procedure diagrams were approved. Existing public diagrams were kept; "
                "check the AI asset settings and source-page matching before importing again."
            )
        write_manifest(args.output, candidates)
    if args.to_db:
        replace_system_codes = verified_report_codes() if args.from_reports else None
        stats = import_to_database(candidates, replace=args.replace, replace_system_codes=replace_system_codes)
        print(f"Imported {stats['imported']} asset candidates, skipped {stats['skipped']} unmatched candidates.")
    if args.from_reports:
        print(f"Built {len(candidates)} diagram candidates from verified reports -> {args.output}")
    elif args.from_manifest:
        print(f"Loaded {len(candidates)} asset candidates from {args.output}")
    else:
        print(f"Built {len(candidates)} asset candidates -> {args.output}")


if __name__ == "__main__":
    main()
