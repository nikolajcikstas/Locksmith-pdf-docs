from __future__ import annotations

import argparse
import json
from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.repository import LocksmithRepository


def import_vehicle_candidates(path: Path, min_confidence: float = 0.55, enqueue_only: bool = True) -> dict:
    repo = LocksmithRepository()
    payload = json.loads(path.read_text(encoding="utf-8"))
    imported = 0
    enqueued = 0
    skipped = 0

    for candidate in payload.get("vehicle_applications", []):
        if float(candidate.get("confidence") or 0) < min_confidence:
            skipped += 1
            continue
        if enqueue_only:
            repo.enqueue_review(
                item_type="vehicle_application",
                item_ref=f"{candidate['source_document']}:{candidate['source_page']}:{candidate['make']}:{candidate['model']}",
                title=f"{candidate['make']} {candidate['model']} {candidate['year_from']}-{candidate['year_to']} -> {candidate['system_code']}",
                payload=candidate,
            )
            enqueued += 1
        else:
            repo.upsert_vehicle(
                {
                    "make": candidate["make"],
                    "model": candidate["model"],
                    "year_from": candidate["year_from"],
                    "year_to": candidate["year_to"],
                    "system_code": candidate["system_code"],
                    "type": candidate.get("system_type"),
                    "source_document": candidate.get("source_document"),
                    "source_pages": [candidate.get("source_page")],
                }
            )
            imported += 1

    return {"imported": imported, "enqueued": enqueued, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    input_path = Path(args.input) if args.input else settings.data_dir / "toc_candidates.json"
    result = import_vehicle_candidates(
        path=input_path,
        min_confidence=args.min_confidence,
        enqueue_only=not args.approve,
    )
    print(result)


if __name__ == "__main__":
    main()
