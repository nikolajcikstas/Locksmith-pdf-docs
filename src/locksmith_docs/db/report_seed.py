from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.connection import get_connection


def bundled_report_seed_path() -> Path:
    return get_settings().project_root / "seed" / "report_drafts.json"


def bundled_parser_seed_path() -> Path:
    return get_settings().project_root / "seed" / "parser_candidates.json"


def ensure_bundled_report_seed() -> bool:
    """Restore approved report drafts packaged with the app image.

    The seed contains only rewritten, publication-approved reports. It does not
    include source PDFs, raw OCR text, or page images.
    """
    settings = get_settings()
    destination = settings.data_dir / "report_drafts.json"
    source = bundled_report_seed_path()
    if destination.exists() or not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return True


def ensure_bundled_parser_seed() -> bool:
    """Restore packaged OCR section candidates when source PDFs are not present."""
    settings = get_settings()
    destination = settings.data_dir / "parser_candidates.json"
    source = bundled_parser_seed_path()
    if destination.exists() or not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return True


def export_published_report_seed(path: Path | None = None) -> Path:
    settings = get_settings()
    destination = path or bundled_report_seed_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    retained: dict[str, dict[str, Any]] = {}
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
        for item in existing if isinstance(existing, list) else []:
            code = str(item.get("system_code") or "")
            if code and item.get("ai_verified") and not item.get("publication_issues") and isinstance(item.get("draft"), dict):
                retained[code] = item
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT system_code, source_document, source_pages, draft, confidence,
                   ai_verified, publication_issues
            FROM report_drafts
            WHERE status = 'published'
              AND ai_verified = true
              AND publication_issues = '[]'::jsonb
            ORDER BY system_code, source_document, source_pages
            """
        ).fetchall()
    for row in rows:
        retained[row["system_code"]] = {
            "system_code": row["system_code"],
            "source_document": row["source_document"],
            "source_pages": row["source_pages"] or [],
            "confidence": float(row["confidence"] or 0),
            "ai_verified": bool(row["ai_verified"]),
            "publication_issues": row["publication_issues"] or [],
            "draft": row["draft"] or {},
        }
    payload = [retained[code] for code in sorted(retained)]
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination
