# Locksmith Vehicle Docs

Python web application for a subscription locksmith vehicle documentation workflow.

## What It Does

- Accepts `Make`, `Model`, and `Year`.
- Finds the matching vehicle system code, for example `CHRY-1`.
- Builds an English locksmith report from structured data.
- Displays original in-app diagrams only when a source visual explains a physical procedure.
- Renders tables and notes as HTML from structured data instead of showing table screenshots.
- Keeps source PDF pixels internal; public diagrams are redrawn from extracted facts.

## How Data Is Stored

The local fallback can still read `data/catalog.json`, but the application data store is PostgreSQL.
See `docs/PRODUCT_ARCHITECTURE.md` for the current product/data flow.

Main data groups:

- `documents`: source PDF metadata.
- `document_pages`: OCR text and page image references with full-text indexes.
- `vehicle_applications`: make/model/year ranges mapped to a system code.
- `systems`: approved structured technical report data.
- `report_drafts`: OCR-derived structured report drafts retained for traceability.
- `assets`: extracted diagram data and generated public schematic references attached to a system.
- `review_queue`: legacy internal queue; not part of the current public workflow.

The lookup flow is:

```text
Make + Model + Year -> vehicle_applications -> system_code -> system report
```

## Public Image Rules

The public report never shows PDF screenshots or source crops. A source visual is considered only when it clarifies an operation; the importer extracts its facts and draws a new diagram in the application's own style.

Eligible diagram sources:

- pin, wafer, or cut-position maps;
- door, trunk, ignition, valet, or glove-box coverage diagrams;
- physical procedure diagrams where the visual relationship is needed to perform the step.

Rendered as HTML instead:

- key cut data;
- spacing/depth tables;
- programmer support;
- transponder notes;
- STRATTEC/ASP lock tables;
- making-key procedures;
- warnings.

Plain key/fob photos, decorative images, tables, headers, and illegible visuals are rejected.

## Diagram Processing

`locksmith_docs.media.extract_assets` finds probable operational diagrams on internal source pages. The AI reviewer returns structured panel/grid data only; the server stores it in `assets.diagram_data` and renders a new SVG schematic for the customer report. Source images and legacy raster candidates are not served publicly.

## OCR And AI Cleanup

The importer now chooses between native PDF text and multiple Tesseract OCR passes instead of blindly concatenating them. That prevents duplicate garbage text from leaking into reports.

Reports are published only after AI verification. Put a newly issued OpenAI key in a local `.env` file before starting Docker; `.env` is ignored by Git and must never be committed:

```cmd
copy /Y .env.example .env
notepad .env
docker compose up --build
```

In Notepad, set `OPENAI_API_KEY=` to the key value, save the file, then start Docker. A leaked or previously shared key should be revoked and replaced before use.
The web container loads secrets from `.env` directly, so stale terminal environment variables cannot override the saved API key.

The AI review reads the page images to repair damaged OCR, retain supported technical identifiers, fill available report sections, and reject incomplete or corrupted reports from public output. With AI review enabled, operational source diagrams are converted to original in-app schematics. Without a working key, a publication rebuild stops immediately and keeps the previous public catalog instead of publishing unchecked OCR text.

### Cost-Controlled Import

AI is used only during document ingestion and verification. Customer searches and report views always read approved PostgreSQL data and generated SVG files; they make no OpenAI requests.

- Local OCR runs before AI and remains enabled without per-page API cleanup (`OCR_AI_CLEANUP=0`).
- Report verification processes at most `AI_MAX_NEW_REPORTS_PER_RUN` uncached systems per rebuild; the pilot default is `1` so report quality and spending can be reviewed before expanding a batch.
- `OPENAI_REPORT_MAX_OUTPUT_TOKENS=12000` is a ceiling for long structured reports so a valid JSON result is not cut off; billing reflects actual output, not the ceiling.
- `OPENAI_REPORT_TIMEOUT_SECONDS=600` allows a long one-time ingestion report to finish instead of discarding its answer after two minutes.
- Verified and rejected AI results are cached by source content and parser version, so an unchanged PDF section is not repeatedly billed.
- The report verification request also extracts any useful procedure-diagram facts from the most likely procedure page in its source section. Normal rebuilds draw SVG schematics from those facts without a separate image-AI pass.
- `Deep Diagram Recovery` is an optional additional AI scan for missed visuals and should be used only after report coverage has been reviewed.
- Optional deep diagram recovery processes at most `AI_MAX_NEW_DIAGRAMS_PER_RUN` new source pages per click; the default is `5`, and saved accepted diagrams are never removed by a later scan.
- `REPORT_AI_RETRY_ON_FAILURE=0` avoids automatically paying for a second correction call on a report that failed validation.
- The admin AI-connection test validates API access without generating report text.

Set `OCR_AI_CLEANUP=1`, `REPORT_AI_RETRY_ON_FAILURE=1`, or use deep diagram recovery only when deliberately spending additional API budget to repair a specific import problem.

### Clean Pilot Import

Before importing a large library, validate one representative book through `/admin` using **Clean Pilot Import**. The pilot preserves all uploaded PDF originals, archives prior OCR/report/diagram output, clears generated database records, then processes only the selected PDF. For the initial European catalog test, select `Audi-VW-Porsche.pdf`, review its published models and reports, and expand to the other books only after the output quality is accepted.

## Run The Application

Start PostgreSQL and the FastAPI web app:

```powershell
docker compose up --build
```

Load the parsed PDF/OCR data:

```cmd
docker compose exec web python -m locksmith_docs.db.bootstrap
```

For fast reruns after OCR pages are already loaded:

```cmd
docker compose exec web python -m locksmith_docs.db.bootstrap --skip-ocr-pages
```

Open:

```text
http://localhost:8000
http://localhost:8000/blog
```

## Run Without Docker

```powershell
& "C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" app.py
```

Then open:

```text
http://127.0.0.1:8000
```

For testing from another computer on the same network, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_public.ps1
```

Then open the machine IP on port `8001`, for example:

```text
http://100.100.27.237:8001
```

Try:

```text
Make: Chrysler
Model: 300
Year: 2018
```

## Next Steps

- Re-run full OCR after parser changes whenever old reports still show previous OCR output.
- Expand model normalization dictionaries by brand.
- Improve report rendering and parser rules by brand as testers find bad rows.
- Add user accounts and subscriptions.
- Add Stripe or another payment provider after the technical data flow is stable.
