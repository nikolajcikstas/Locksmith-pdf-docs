from __future__ import annotations

import re
from typing import Iterable

from locksmith_docs.parsing.models import VehicleApplicationCandidate


YEAR_RE = re.compile(r"(?P<start>(?:19|20)\d{2})\s*[-\u2013]\s*(?P<end>\d{2,4})")
SECTION_RE = re.compile(r"\b(?P<section>[A-Z]{2,5}[-\u2013][A-Z0-9]{1,4})\b")
TYPE_WORDS = ("Proximity", "Fobik", "Key", "Remote", "Transponder")


def normalize_year_range(text: str) -> tuple[int, int] | None:
    match = YEAR_RE.search(text)
    if not match:
        return None
    start = int(match.group("start"))
    if not 1900 <= start <= 2035:
        return None
    raw_end = match.group("end")
    if len(raw_end) == 2:
        end = int(str(start)[:2] + raw_end)
        if end < start:
            end += 100
    elif len(raw_end) == 3:
        end = int(str(start)[:2] + raw_end[:2])
    else:
        end = int(raw_end)
    if end < start or end > 2035 or end - start > 50:
        return None
    return start, end


def detect_type(text: str) -> str:
    lowered = text.lower()
    for word in TYPE_WORDS:
        if word.lower() in lowered:
            return word
    return "Key"


def parse_toc_lines(
    lines: Iterable[str],
    source_document: str,
    source_page: int,
    active_make: str | None = None,
) -> list[VehicleApplicationCandidate]:
    candidates: list[VehicleApplicationCandidate] = []
    make = active_make or ""

    for line in lines:
        clean = " ".join(line.split())
        if not clean:
            continue
        if clean.isupper() and len(clean.split()) <= 3 and not YEAR_RE.search(clean):
            make = clean.title()
            continue

        year_range = normalize_year_range(clean)
        section_match = SECTION_RE.search(clean)
        if not year_range or not section_match or not make:
            continue

        model_part = clean[: clean.find(str(year_range[0]))].strip(" -")
        if not model_part:
            continue
        model = re.sub(r"\s+", " ", model_part).strip()
        section = section_match.group("section").replace("\u2013", "-")
        system_type = detect_type(clean)

        candidates.append(
            VehicleApplicationCandidate(
                make=make,
                model=model,
                year_from=year_range[0],
                year_to=year_range[1],
                system_type=system_type,
                system_code=section,
                source_document=source_document,
                source_page=source_page,
                confidence=0.55,
            )
        )

    return candidates
