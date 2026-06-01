from __future__ import annotations

import re
from collections import defaultdict

from locksmith_docs.parsing.models import OcrWord, VehicleApplicationCandidate
from locksmith_docs.parsing.toc_parser import detect_type, normalize_year_range


DEFAULT_TOC_COLUMNS = {
    "model": (0.22, 0.48),
    "years": (0.48, 0.62),
    "type": (0.62, 0.73),
    "section": (0.73, 0.86),
    "page": (0.86, 0.98),
}

SECTION_CLEAN_RE = re.compile(r"[^A-Z0-9-]")


def group_words_by_row(words: list[OcrWord], y_tolerance: int = 18) -> list[list[OcrWord]]:
    sorted_words = sorted(words, key=lambda word: (word.mid_y, word.left))
    rows: list[list[OcrWord]] = []
    row_midpoints: list[float] = []

    for word in sorted_words:
        for index, midpoint in enumerate(row_midpoints):
            if abs(word.mid_y - midpoint) <= y_tolerance:
                rows[index].append(word)
                row_midpoints[index] = (midpoint + word.mid_y) / 2
                break
        else:
            rows.append([word])
            row_midpoints.append(word.mid_y)

    return [sorted(row, key=lambda word: word.left) for row in rows]


def row_to_columns(row: list[OcrWord], page_width: int, columns: dict[str, tuple[float, float]] | None = None) -> dict[str, str]:
    col_defs = columns or DEFAULT_TOC_COLUMNS
    values: dict[str, list[str]] = defaultdict(list)
    for word in row:
        x_ratio = word.mid_x / page_width
        for name, (start, end) in col_defs.items():
            if start <= x_ratio < end:
                values[name].append(word.text)
                break
    return {name: " ".join(parts).strip() for name, parts in values.items()}


def normalize_section(value: str) -> str:
    upper = SECTION_CLEAN_RE.sub("", value.upper().replace("_", "-"))
    match = re.search(r"[A-Z]{2,5}-?[A-Z0-9]{1,4}", upper)
    if not match:
        return upper
    section = match.group(0)
    if "-" not in section and len(section) > 4:
        section = section[:4] + "-" + section[4:]
    return section


def parse_toc_words(
    words: list[OcrWord],
    page_width: int,
    make: str,
    source_document: str,
    source_page: int,
) -> list[VehicleApplicationCandidate]:
    candidates: list[VehicleApplicationCandidate] = []
    current_model = ""

    for row in group_words_by_row(words):
        columns = row_to_columns(row, page_width)
        model = columns.get("model", "").strip()
        years = columns.get("years", "").strip()
        section = columns.get("section", "").strip()
        row_text = " ".join(word.text for word in row)

        if model and not normalize_year_range(model) and model.lower() not in {"model", "make"}:
            current_model = model

        year_range = normalize_year_range(years or row_text)
        if not year_range or not current_model or not section:
            continue

        candidates.append(
            VehicleApplicationCandidate(
                make=make,
                model=current_model,
                year_from=year_range[0],
                year_to=year_range[1],
                system_type=detect_type(columns.get("type", row_text)),
                system_code=normalize_section(section),
                source_document=source_document,
                source_page=source_page,
                confidence=_confidence(row),
            )
        )

    return candidates


def _confidence(row: list[OcrWord]) -> float:
    if not row:
        return 0.0
    avg = sum(word.confidence for word in row) / len(row)
    return round(max(0.0, min(0.95, avg / 100)), 2)
