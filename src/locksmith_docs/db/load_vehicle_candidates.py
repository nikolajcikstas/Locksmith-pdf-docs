from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.repository import LocksmithRepository
from locksmith_docs.parsing.section_parser import normalize_section_code


def load_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("vehicle_applications", []))


def title_for(candidate: dict[str, Any]) -> str:
    return (
        f"{candidate['make']} {candidate['model']} "
        f"{candidate['year_from']}-{candidate['year_to']} -> {candidate['system_code']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Load parsed vehicle/system candidates into Postgres.")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--approve-min-confidence", type=float, default=0.82)
    parser.add_argument("--review-below", action="store_true")
    parser.add_argument("--replace-source-docs", action="store_true", help="Delete previously imported PDF-derived vehicle links before loading.")
    args = parser.parse_args()

    settings = get_settings()
    input_path = args.input or settings.data_dir / "parser_candidates.json"
    repo = LocksmithRepository()
    if args.replace_source_docs:
        repo.delete_imported_vehicle_links()

    approved = 0
    queued = 0
    for candidate in load_candidates(input_path):
        system_code = normalize_section_code(candidate.get("system_code", ""), candidate.get("source_document", ""))
        if not system_code:
            continue
        confidence = float(candidate.get("confidence") or 0)
        if confidence >= args.approve_min_confidence:
            repo.upsert_vehicle(
                {
                    "make": candidate["make"],
                    "model": candidate["model"],
                    "year_from": candidate["year_from"],
                    "year_to": candidate["year_to"],
                    "system_code": system_code,
                    "type": candidate.get("system_type"),
                    "source_document": candidate.get("source_document"),
                    "source_pages": [candidate.get("source_page")] if candidate.get("source_page") else [],
                }
            )
            approved += 1
        elif args.review_below:
            repo.enqueue_review(
                item_type="vehicle_application",
                item_ref=f"{candidate.get('source_document')}:{candidate.get('source_page')}:{candidate.get('system_code')}:{candidate.get('make')}:{candidate.get('model')}",
                title=title_for(candidate),
                payload=candidate,
            )
            queued += 1

    print(f"Approved vehicle links: {approved}")
    print(f"Queued for review: {queued}")


if __name__ == "__main__":
    main()
