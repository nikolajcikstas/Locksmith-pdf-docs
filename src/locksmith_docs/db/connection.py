from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection
from psycopg.rows import dict_row

from locksmith_docs.core.config import get_settings


@contextmanager
def get_connection() -> Iterator[Connection]:
    settings = get_settings()
    with Connection.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn
