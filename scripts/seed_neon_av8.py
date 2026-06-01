"""Publish only AV-8 (Audi A3 / Q3) into Neon — fast deploy seed, no full bootstrap."""
from __future__ import annotations

import json
from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.init_schema import init_schema
from locksmith_docs.db.repository import LocksmithRepository
from locksmith_docs.reports.verified_facts import apply_verified_facts


def load_av8_draft(data_dir: Path) -> dict:
    cache_path = data_dir / "ai_report_cache.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    for item in payload.values():
        if isinstance(item, dict) and item.get("code") == "AV-8":
            return apply_verified_facts(item, "AV-8", data_dir / "verified_report_facts.json")
    raise SystemExit("AV-8 draft not found in data/ai_report_cache.json")


def main() -> None:
    settings = get_settings()
    print("Initializing schema...")
    init_schema()

    draft = load_av8_draft(settings.data_dir)
    system = dict(draft)
    system["type"] = system.get("system_type") or system.get("type") or ""

    repo = LocksmithRepository()
    print("Publishing system AV-8...")
    repo.upsert_catalog_system(system, publication_source="ai_verified")

    apps = draft.get("vehicle_applications") or []
    print(f"Publishing {len(apps)} vehicle application(s)...")
    repo.replace_verified_vehicle_applications(
        system_code=system["code"],
        applications=apps,
        source_document="Audi-VW-Porsche.pdf",
        source_pages=[18, 19],
        system_type=system.get("type"),
    )
    print("Done. AV-8 is live in Neon.")


if __name__ == "__main__":
    main()
