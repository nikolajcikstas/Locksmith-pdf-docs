# Product Architecture

## Public Flow

The customer-facing product has two public areas:

- Vehicle Lookup: make, model, and year search.
- Blog & Videos: training articles and YouTube video cards.

Admin approval screens are removed from the public app. Imported reports are published directly into the searchable catalog during bootstrap, then corrected by improving parser rules and re-running the import.

## Database Model

PostgreSQL is the source of truth.

- `documents`: source PDF metadata.
- `document_pages`: every OCR page, page image path, full-text vector, trigram search.
- `vehicle_applications`: normalized make/model/year ranges mapped to a system code.
- `systems`: structured report JSON used by the public report.
- `assets`: cleaned public key/blade/fob/procedure image crops.
- `blog_posts`: article content.
- `videos`: YouTube topic slots or resolved YouTube video IDs.
- `report_drafts`: trace copies of parser-generated reports.

Lookup path:

```text
make + model + year -> vehicle_applications -> system_code -> systems + assets
```

## Report Generation

The importer scans all OCR pages already stored in `document_pages` / `data/imported_pages.json`.

For each detected system section it extracts:

- key / remote options;
- FCC IDs and frequency;
- emergency blade and keyway references;
- code series, MACS, spacing, depths;
- transponder and chip notes;
- programmer and diagnostic notes;
- decoder / reader references;
- field workflow and service notes;
- warnings and troubleshooting hints.

Text from source pages is not displayed as a raw copy. The parser normalizes OCR noise, keeps the technical meaning, and rewrites the output into field-report language.

Tables from the source are rendered as HTML tables. Text-heavy screenshots are not shown as images.

## Image Strategy

The selected strategy is real source-image extraction, not generated schematics.

`locksmith_docs.media.extract_assets` scans page images and crops likely operational images:

- key references;
- fobs;
- emergency blades;
- lock/procedure reference images.

Then it applies `remove_light_watermark()` to suppress pale watermark pixels and publishes only crops marked as `procedure_image`.

Rules:

- Public images must help identify or perform a locksmith task.
- Decorative images are discarded.
- Text-heavy images are converted into text/tables instead of being shown.
- The report layout keeps image cards contained and proportional.

Current image decision:

```text
PDF page image
-> skip intro/table-of-contents/decorative pages
-> detect visual regions by dark/saturated connected areas
-> reject text-heavy/table-like crops
-> keep only key/blade/fob/lock/procedure regions
-> trim white border
-> remove pale watermark pixels
-> save public crop under static/assets/candidates
-> attach it to the parsed system code
```

This is intentionally one strategy: use cleaned source crops. The app does not mix in generated diagrams for public reports.

## Video Blog

The blog uses the app endpoint:

```text
GET /api/blog/videos
```

The frontend fetches this JSON and renders a responsive two-column YouTube grid.

If `YOUTUBE_API_KEY` is configured, the bootstrap can refresh seeded topic slots with real YouTube video IDs through the YouTube API. Without a key, the app uses YouTube search embeds for the configured locksmith topics.

## Search Performance

Important indexes:

- make/model/year lookup index on `vehicle_applications`;
- trigram indexes for fuzzy make/model cleanup;
- full-text GIN index on OCR pages;
- public asset index by system;
- published video index.

This keeps the public lookup fast while still allowing fallback OCR search when a clean report mapping is missing.
