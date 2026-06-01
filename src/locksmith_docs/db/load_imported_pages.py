from __future__ import annotations

import json
from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.repository import LocksmithRepository


def load_imported_pages(path: Path | None = None) -> None:
    settings = get_settings()
    data_path = path or settings.data_dir / "imported_pages.json"
    repo = LocksmithRepository()
    imported = json.loads(data_path.read_text(encoding="utf-8"))

    for document in imported:
        if document.get("missing"):
            continue
        document_id = repo.upsert_document(
            name=document["name"],
            source_path=document["path"],
            pages_total=document["pages_total"],
        )
        for page in document.get("pages", []):
            repo.upsert_page(document_id, page)


if __name__ == "__main__":
    load_imported_pages()
