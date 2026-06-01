from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

from locksmith_docs.core.config import get_settings
from locksmith_docs.parsing.geometry_toc import parse_toc_words
from locksmith_docs.parsing.tesseract_ocr import preprocess_for_toc, run_tesseract_tsv


def build_from_jobs(jobs_path: Path) -> dict:
    settings = get_settings()
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    candidates = []
    page_stats = []

    for job in jobs:
        for page in job["pages"]:
            image_path = settings.storage_dir / "pages" / job["storage_slug"] / f"page_{page:04d}.jpg"
            if not image_path.exists():
                page_stats.append({"document": job["document"], "page": page, "error": "missing image"})
                continue
            processed = preprocess_for_toc(image_path)
            try:
                words = run_tesseract_tsv(processed)
                width = Image.open(processed).width
                page_candidates = parse_toc_words(
                    words=words,
                    page_width=width,
                    make=job["make"],
                    source_document=job["document"],
                    source_page=page,
                )
                candidates.extend(candidate.__dict__ for candidate in page_candidates)
                page_stats.append(
                    {
                        "document": job["document"],
                        "page": page,
                        "words": len(words),
                        "candidates": len(page_candidates),
                    }
                )
            finally:
                processed.unlink(missing_ok=True)

    return {
        "stats": {
            "pages": len(page_stats),
            "vehicle_candidates": len(candidates),
        },
        "page_stats": page_stats,
        "vehicle_applications": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = get_settings()
    jobs_path = Path(args.jobs) if args.jobs else settings.data_dir / "toc_jobs.json"
    out_path = Path(args.out) if args.out else settings.data_dir / "toc_candidates.json"

    result = build_from_jobs(jobs_path)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(out_path)
    print(result["stats"])


if __name__ == "__main__":
    main()
