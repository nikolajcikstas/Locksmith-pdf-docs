from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_host: str
    app_port: int
    project_root: Path
    storage_dir: Path
    data_dir: Path


def get_settings() -> Settings:
    project_root = Path(__file__).resolve().parents[3]
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs",
        ),
        app_host=os.environ.get("APP_HOST", "127.0.0.1"),
        app_port=int(os.environ.get("APP_PORT", "8000")),
        project_root=project_root,
        storage_dir=project_root / "storage",
        data_dir=project_root / "data",
    )
