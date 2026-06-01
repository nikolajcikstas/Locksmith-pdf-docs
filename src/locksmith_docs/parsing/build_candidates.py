from __future__ import annotations

import argparse
import json
from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.parsing.section_vehicle_parser import extract_vehicle_applications_from_sections
from locksmith_docs.parsing.section_parser import split_sections
from locksmith_docs.parsing.toc_parser import parse_toc_lines


def build_candidates(imported_pages_path: Path) -> dict:
    imported = json.loads(imported_pages_path.read_text(encoding="utf-8"))
    vehicle_candidates = []
    section_candidates = []

    for document in imported:
        if document.get("missing"):
            continue
        document_name = document["name"]
        pages = document.get("pages", [])

        for page in pages:
            text = page.get("ocr_text") or ""
            lines = [line for line in text.splitlines() if line.strip()]
            # Windows OCR often flattens tables into a single line. Splitting on long spaces
            # is still useful when higher-quality OCR is plugged in later.
            vehicle_candidates.extend(
                candidate.__dict__
                for candidate in parse_toc_lines(
                    lines=lines,
                    source_document=document_name,
                    source_page=page["page_number"],
                )
            )

        section_candidates.extend(section.__dict__ for section in split_sections(pages, document_name))

    vehicle_candidates.extend(
        candidate.__dict__
        for candidate in extract_vehicle_applications_from_sections(section_candidates)
    )

    return {
        "vehicle_applications": vehicle_candidates,
        "sections": section_candidates,
        "stats": {
            "vehicle_candidates": len(vehicle_candidates),
            "section_candidates": len(section_candidates),
        },
    }


def enqueue_candidates(candidates: dict) -> None:
    from locksmith_docs.db.repository import LocksmithRepository

    repo = LocksmithRepository()
    for idx, item in enumerate(candidates.get("vehicle_applications", []), start=1):
        repo.enqueue_review(
            item_type="vehicle_application",
            item_ref=f"{item['source_document']}:{item['source_page']}:{idx}",
            title=f"{item['make']} {item['model']} {item['year_from']}-{item['year_to']} -> {item['system_code']}",
            payload=item,
        )
    for item in candidates.get("sections", []):
        repo.enqueue_review(
            item_type="system_section",
            item_ref=f"{item['source_document']}:{item['page_start']}:{item['code']}",
            title=f"{item['code']} from {item['source_document']} pages {item['page_start']}-{item['page_end']}",
            payload=item,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--enqueue", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    input_path = Path(args.input) if args.input else settings.data_dir / "imported_pages.json"
    output_path = Path(args.output) if args.output else settings.data_dir / "parser_candidates.json"

    candidates = build_candidates(input_path)
    output_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output_path)
    print(candidates["stats"])

    if args.enqueue:
        enqueue_candidates(candidates)


if __name__ == "__main__":
    main()
