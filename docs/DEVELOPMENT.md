# Development

## Local Docker

```powershell
cd locksmith_mvp
docker compose up --build
```

App:

```text
http://localhost:8000
```

Postgres:

```text
postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs
```

## Load Existing OCR JSON Into Postgres

```powershell
$env:DATABASE_URL="postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs"
$env:PYTHONPATH="src"
python -m locksmith_docs.db.bootstrap
```

For faster reruns after OCR pages are already loaded:

```powershell
$env:DATABASE_URL="postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs"
$env:PYTHONPATH="src"
python -m locksmith_docs.db.bootstrap --skip-ocr-pages
```

To refresh the six blog video slots through the YouTube Data API, set a key before running bootstrap:

```powershell
$env:YOUTUBE_API_KEY="your-youtube-data-api-key"
python -m locksmith_docs.db.bootstrap --skip-ocr-pages
```

Without `YOUTUBE_API_KEY`, the app keeps seeded fallback video cards and still renders YouTube embeds/links in the blog.
The fallback uses six topic-specific YouTube search links, so the blog is still useful without an API token.

Manual equivalent:

```powershell
python -m locksmith_docs.db.load_imported_pages
python -m locksmith_docs.db.load_catalog
python -m locksmith_docs.db.load_blog_seed
python -m locksmith_docs.parsing.build_candidates
python -m locksmith_docs.db.load_vehicle_candidates --replace-source-docs
python -m locksmith_docs.reports.build_drafts --to-db --publish
```

## Build TOC Vehicle Candidates With Tesseract

This requires the Docker image or a local Tesseract installation.

```powershell
$env:PYTHONPATH="src"
python -m locksmith_docs.parsing.build_toc_candidates
python -m locksmith_docs.parsing.quality_report
python -m locksmith_docs.parsing.import_candidates
```

By default, `import_candidates` enqueues vehicle candidates for admin review.
Use `--approve` only after the parser quality report looks clean.

## Build Vehicle Links From Sections

The first parser tries TOC pages. The fallback parser reads the beginning of each system section and extracts rows like `2016-21 ILX (Acura)` directly from the section text. This works even when the scanned table of contents is too dense for Windows OCR.

Model names are normalized through `locksmith_docs.parsing.canonical_models` before insertion. That keeps common OCR errors like `PiIot`, `0utback`, or `GaIant` from becoming separate filter options.

```powershell
$env:PYTHONPATH="src"
python -m locksmith_docs.parsing.build_candidates
python -m locksmith_docs.parsing.quality_report --input data/parser_candidates.json --out docs/PARSER_QUALITY.md
```

With Postgres running:

```powershell
$env:DATABASE_URL="postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs"
python -m locksmith_docs.db.load_vehicle_candidates --replace-source-docs
```

Vehicle links at or above `0.75` confidence are inserted into `vehicle_applications`.
The current workflow does not require admin approval; bad rows are fixed by improving parser rules and rerunning the loader.

## Build Report Drafts

OCR section candidates are converted into structured report drafts before anything is published to locksmiths.
This keeps low-quality OCR out of the customer-facing report until an admin reviews it.

```powershell
$env:PYTHONPATH="src"
python -m locksmith_docs.reports.build_drafts
```

Output:

```text
data/report_drafts.json
```

With Postgres running, insert drafts and publish customer-facing systems:

```powershell
$env:DATABASE_URL="postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs"
$env:PYTHONPATH="src"
python -m locksmith_docs.reports.build_drafts --to-db --publish
```

## Extract Image Candidates

Images are handled as internal review candidates. The extractor skips page headers and text-heavy/table content, removes light watermark noise from accepted crops, and stores candidates as internal assets. Nothing becomes customer-visible until `visibility='public'` and `review_status='approved'`.

```powershell
$env:PYTHONPATH="src"
python -m locksmith_docs.media.extract_assets
```

For a quick smoke test:

```powershell
python -m locksmith_docs.media.extract_assets --limit-pages 200
```

With Postgres running:

```powershell
$env:DATABASE_URL="postgresql://locksmith:locksmith_dev@localhost:5432/locksmith_docs"
python -m locksmith_docs.media.extract_assets --to-db
```

## Current Limitation

The checked-in desktop MVP can still run without Postgres through `app.py`.
The production scaffold under `src/locksmith_docs` expects Postgres and the dependencies in `requirements.txt`.

## OCR Note

The current Windows OCR import is enough to find section candidates and source pages, but it is not enough for reliable table-of-contents extraction.
AutoSmart TOC pages are dense scanned tables; Windows OCR often merges columns into one line.

For production-grade make/model/year extraction, use OCR with bounding boxes:

- Tesseract TSV/HOCR in Docker;
- Google Vision;
- AWS Textract;
- Azure Document Intelligence.

The parser already has a column-based TOC path in `locksmith_docs.parsing.column_toc`, but it needs higher-quality OCR output to reliably align model/year/type/section/page rows.

See `docs/OCR_TOC_PIPELINE.md` for the Tesseract TSV workflow.
