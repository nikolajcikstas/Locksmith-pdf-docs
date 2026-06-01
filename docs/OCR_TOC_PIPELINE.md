# OCR TOC Pipeline

The vehicle dropdown depends on table-of-contents extraction:

```text
Make + Model + Years + Type + Section + Page
```

Windows OCR can read page text, but it often merges dense table columns. The production path uses Tesseract TSV output because TSV includes bounding boxes for each word.

## Flow

```text
page image
-> upscale / threshold / sharpen
-> tesseract TSV
-> group words into rows by y-coordinate
-> assign words to TOC columns by x-coordinate
-> build vehicle_application candidates
-> quality report
-> review queue
-> approved vehicle_applications
```

## Commands

Inside Docker or any environment with Tesseract installed:

```powershell
$env:PYTHONPATH="src"
python -m locksmith_docs.parsing.build_toc_candidates
python -m locksmith_docs.parsing.quality_report
python -m locksmith_docs.parsing.import_candidates
```

To approve automatically after checking quality:

```powershell
python -m locksmith_docs.parsing.import_candidates --approve --min-confidence 0.75
```

## Files

- Jobs: `data/toc_jobs.json`
- Output: `data/toc_candidates.json`
- Report: `docs/PARSER_QUALITY.md`
- Parser: `src/locksmith_docs/parsing/geometry_toc.py`
- Tesseract wrapper: `src/locksmith_docs/parsing/tesseract_ocr.py`

## Tuning

If a page has weak results, tune:

- preprocessing threshold in `tesseract_ocr.py`;
- TOC column boundaries in `geometry_toc.py`;
- page list and make mapping in `data/toc_jobs.json`.

The parser intentionally produces candidates, not final customer data. Review is still required before publishing.
