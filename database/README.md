# Database

The production target is PostgreSQL.

Use `schema.sql` to create the core tables:

```powershell
psql "$env:DATABASE_URL" -f database/schema.sql
```

The current desktop environment does not have a PostgreSQL server or Python Postgres driver installed, so the running MVP still reads `data/catalog.json` and `data/imported_pages.json`.
The schema is ready for the next step: loading OCR pages, approved vehicle applications, approved systems, and reviewed assets into Postgres.

Expected environment variable:

```text
DATABASE_URL=postgresql://user:password@localhost:5432/locksmith_docs
```
