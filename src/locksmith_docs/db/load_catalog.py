from __future__ import annotations

import json
from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.repository import LocksmithRepository


def load_catalog(path: Path | None = None) -> None:
    settings = get_settings()
    data_path = path or settings.data_dir / "catalog.json"
    repo = LocksmithRepository()
    catalog = json.loads(data_path.read_text(encoding="utf-8"))

    for system in catalog.get("systems", []):
        repo.upsert_catalog_system(system)
    for vehicle in catalog.get("vehicles", []):
        repo.upsert_vehicle(vehicle)


if __name__ == "__main__":
    load_catalog()
