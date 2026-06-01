from __future__ import annotations

from pathlib import Path

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.connection import get_connection


def init_schema(path: Path | None = None) -> None:
    settings = get_settings()
    schema_path = path or settings.project_root / "database" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection() as conn:
        for statement in split_statements(sql):
            conn.execute(statement)
        conn.commit()


def split_statements(sql: str) -> list[str]:
    statements = []
    current = []
    in_single_quote = False
    for char in sql:
        if char == "'":
            in_single_quote = not in_single_quote
        if char == ";" and not in_single_quote:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


if __name__ == "__main__":
    init_schema()
