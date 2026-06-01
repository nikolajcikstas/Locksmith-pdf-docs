# MVP Architecture

## Product Flow

The app is built around one locksmith question:

```text
What do I need for this vehicle?
```

The user enters:

```text
Make + Model + Year
```

The app resolves that query to a technical system code:

```text
Chrysler + 300 + 2018 -> CHRY-1
```

Then the report is assembled from structured data attached to `CHRY-1`.

## Text Storage

For the MVP, text is stored in `data/catalog.json`.

The important rule is that text is not stored as PDF pages. It is broken into report sections:

- `quick_answer`
- `key_remote`
- `mechanical_key`
- `transponder`
- `programming`
- `making_key`
- `lock_parts`
- `warnings`
- `assets`

This lets the same technical system be reused by multiple vehicles.

Example:

```text
CHRY-1 applies to:
- Chrysler 300, 2011-2021
- Chrysler 200, 2015-2017
```

The report content is stored once under `CHRY-1`.

## Image Storage

Images are stored as assets attached to a system:

```json
{
  "id": "chry-1-key-position-map",
  "title": "Key Cut Position Map",
  "kind": "useful_explanation",
  "path": "/static/assets/chry-1-key-position-map.png",
  "source_page": 21,
  "visibility": "public"
}
```

Only key images with `visibility: public` and `kind: key_image` are shown in the locksmith report.
Tables, notes, programmer matrices, and STRATTEC data must be extracted into structured text and rendered as HTML, not shown as image crops.

Internal source crops should later be stored separately:

```text
storage/source_crops/{document}/{page}/{asset_id}.png
```

Public cleaned key assets are stored here:

```text
static/assets/{asset_id}.png
```

## Public Image Rule

Public images are limited to key, blade, and fob visuals after watermark cleanup.
All explanatory data is stored as text/JSON and rendered by the app.

Allowed public images:

- key photos;
- fob photos;
- emergency blade photos;
- blank or text-free key visuals.

Not allowed as public images:

- table screenshots;
- programmer support screenshots;
- STRATTEC table screenshots;
- text-heavy diagrams;
- cover graphics, logos, and branding;
- page backgrounds or watermarked source crops.

In the final parser this should be a two-step process:

```text
automatic candidate crop -> remove surrounding text -> watermark cleanup -> human review -> approved key image
```

## Watermark Cleanup

`scripts/extract_asset.py` handles the current MVP cleanup:

1. Open a PDF page.
2. Extract the page raster image.
3. Crop the useful region.
4. Suppress pale cyan/pink watermark pixels.
5. Save a cleaned public asset.

The cleanup is intentionally conservative: it removes light background marks while preserving dark text, boxes, and technical marks.

Example:

```powershell
python scripts/extract_asset.py `
  --pdf "path/to/2021 AutoSmart Domestic US Book.pdf" `
  --page 21 `
  --crop "255,735,835,858" `
  --out "static/assets/chry-1-key-position-map.png"
```

## Filter Logic

The filter reads `vehicles` from `catalog.json`.

For each row:

```json
{
  "make": "Chrysler",
  "model": "300",
  "year_from": 2011,
  "year_to": 2021,
  "system_code": "CHRY-1"
}
```

The app checks:

```text
make matches
model matches
year_from <= requested_year <= year_to
```

Then it loads the matching system report.

## Next Production Step

The MVP proves the report format. The next real step is the parser pipeline:

```text
PDF document
  -> page images
  -> OCR text
  -> table/section detection
  -> extracted structured records
  -> useful image candidates
  -> admin review
  -> approved database
  -> customer report
```

For production, move from `catalog.json` to PostgreSQL with these tables:

- `documents`
- `pages`
- `vehicle_applications`
- `systems`
- `remote_options`
- `mechanical_keys`
- `transponders`
- `programming_tools`
- `making_key_methods`
- `lock_parts`
- `assets`

## Current OCR Import Status

The MVP imports every provided PDF into:

```text
data/imported_pages.json
storage/pages/{document}/page_0001.jpg
```

Each imported page stores:

- document name;
- page number;
- internal page image path;
- OCR text.

The current app uses this OCR corpus as a fallback when a fully structured report is not approved yet:

```text
Make + Model + Year -> no structured record -> search OCR corpus -> show likely source pages
```

This fallback is not the final customer report format. The final report must come from approved structured fields.
