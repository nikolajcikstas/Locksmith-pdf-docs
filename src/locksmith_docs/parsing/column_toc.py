from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from locksmith_docs.core.config import get_settings
from locksmith_docs.parsing.models import VehicleApplicationCandidate
from locksmith_docs.parsing.toc_parser import normalize_year_range


DEFAULT_COLUMN_BOXES = {
    "model": (190, 125, 395, 1080),
    "years": (395, 125, 540, 1080),
    "type": (540, 125, 625, 1080),
    "section": (625, 125, 745, 1080),
    "page": (745, 125, 835, 1080),
}


def clean_ocr_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        clean = " ".join(line.replace("|", " ").split())
        if not clean:
            continue
        if clean.lower() in {"model", "years", "type", "section", "page"}:
            continue
        lines.append(clean)
    return lines


def ocr_image(image_path: Path) -> str:
    settings = get_settings()
    script = settings.project_root / "scripts" / "windows_ocr.ps1"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ImagePath",
            str(image_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def ocr_columns(
    page_image_path: Path,
    column_boxes: dict[str, tuple[int, int, int, int]] | None = None,
) -> dict[str, list[str]]:
    boxes = column_boxes or DEFAULT_COLUMN_BOXES
    image = Image.open(page_image_path).convert("RGB")
    output: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for name, box in boxes.items():
            crop_path = tmp_dir / f"{name}.png"
            crop = image.crop(box)
            crop = crop.resize((crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS)
            crop = crop.convert("L")
            crop = ImageEnhance.Contrast(crop).enhance(1.8)
            crop = crop.filter(ImageFilter.SHARPEN)
            crop = crop.point(lambda px: 255 if px > 190 else 0)
            crop.save(crop_path)
            output[name] = clean_ocr_lines(ocr_image(crop_path))
    return output


def normalize_section(value: str) -> str:
    match = re.search(r"[A-Z]{2,5}[-\u2013]?[A-Z0-9]{1,4}", value.upper())
    if not match:
        return value.strip().upper()
    raw = match.group(0).replace("\u2013", "-")
    if "-" not in raw and len(raw) > 4:
        raw = raw[:4] + "-" + raw[4:]
    return raw


def candidates_from_columns(
    columns: dict[str, list[str]],
    make: str,
    source_document: str,
    source_page: int,
) -> list[VehicleApplicationCandidate]:
    row_count = min(len(columns.get("years", [])), len(columns.get("section", [])))
    candidates: list[VehicleApplicationCandidate] = []
    models = columns.get("model", [])
    types = columns.get("type", [])
    sections = columns.get("section", [])
    years = columns.get("years", [])

    current_model = ""
    model_idx = 0
    for row_idx in range(row_count):
        if model_idx < len(models):
            possible_model = models[model_idx]
            if not normalize_year_range(possible_model) and not re.fullmatch(r"[-\u2013]+", possible_model):
                current_model = possible_model
                model_idx += 1
        year_range = normalize_year_range(years[row_idx])
        if not year_range or not current_model:
            continue
        candidates.append(
            VehicleApplicationCandidate(
                make=make,
                model=current_model,
                year_from=year_range[0],
                year_to=year_range[1],
                system_type=types[row_idx] if row_idx < len(types) else "",
                system_code=normalize_section(sections[row_idx]),
                source_document=source_document,
                source_page=source_page,
                confidence=0.65,
            )
        )
    return candidates
