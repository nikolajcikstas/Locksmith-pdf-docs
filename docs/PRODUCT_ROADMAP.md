# Locksmith Docs Product Roadmap

## Goal

Build a subscription locksmith documentation system where a locksmith enters:

```text
Make + Model + Year
```

and receives a complete field-ready report:

- blade/keyway;
- FCC ID;
- frequency;
- chip/transponder;
- Lishi/decoder tools;
- compatible fobs/remotes;
- spacing/depths;
- programmer support;
- all-keys-lost workflow;
- lock/STRATTEC/ASP parts;
- warnings and troubleshooting;
- approved procedure images or generated diagrams;
- related repair videos.

## Storage Model

Raw imported data:

- `documents`
- `document_pages`
- OCR text
- internal page images

Approved customer data:

- `vehicle_applications`
- `systems`
- `report_sections`
- `assets`
- `videos`
- `blog_posts`

The user-facing report must use approved structured data, not raw OCR.

## Search

Primary lookup:

```sql
make + model + year -> vehicle_applications -> system_code -> systems/report_sections
```

Indexes:

- B-tree functional index for exact make/model/year range lookup.
- `pg_trgm` indexes for fuzzy make/model search and OCR fallback.
- `tsvector` GIN indexes for full-text search over OCR pages and approved systems.

Fallback search:

```text
No approved report -> search OCR corpus -> show likely source pages to admin/reviewer
```

The fallback is useful for building reports, not as final customer output.

## OCR Pipeline

Recommended production OCR:

1. Render PDF pages at high DPI.
2. Deskew and denoise.
3. Run OCR.
4. Store raw OCR and confidence.
5. Detect page type:
   - table of contents;
   - section first page;
   - continued section;
   - chart/table page;
   - image-heavy page.
6. Extract candidates.
7. Push low-confidence items into `review_queue`.

Local MVP currently uses Windows OCR. Production should use Tesseract or a cloud OCR provider for better confidence and bounding boxes.

## Parser Strategy

### Table of Contents Parser

Extracts:

```text
make, model, year_from, year_to, type, system_code, source_page
```

This builds the model dropdown and vehicle lookup.

### Section Parser

Extracts:

```text
system_code
remote/fob options
FCC IDs
frequency
chip/transponder
blade/keyway
spacing/depths
tool support
decoder references
making-key methods
lock parts
warnings
```

The parser should never publish directly. It creates candidates for review.

## Rewrite Algorithm

The rewrite layer works after extraction:

```text
raw OCR/source fact -> structured fact -> rewritten field copy -> approved report
```

Rules:

- Keep exact identifiers unchanged: FCC IDs, part numbers, keyways, chip names, code series, spacing/depth values.
- Change explanatory wording and layout.
- Convert paragraphs into field-ready steps.
- Add explicit sections: prerequisites, action, verification, failure cases.
- Do not publish low-confidence OCR.

Future option:

- Use an LLM for rewriting after facts are extracted.
- Store rewrite source hash and reviewer approval in `rewrite_jobs`.

## Image Algorithm

Images are split into:

- `discard`: decorative, repeated, low-quality, not useful.
- `structured_data`: tables/text/charts converted into HTML tables or generated schemas.
- `procedure_image`: image clarifies a physical operation and may be shown after cleanup/review.

Rules:

- If it contains a table or text, extract the data and render it as HTML.
- If it is a diagram, prefer generated HTML/SVG schema from extracted facts.
- If it is a useful physical-step image, crop surrounding text, remove watermark, then review.
- Low-quality crops are not published.

## Blog And Video

Blog posts are stored in `blog_posts`.

Videos are stored in `videos` with:

```text
youtube_video_id
make
model
year_from
year_to
system_code
tags
```

The app embeds videos through YouTube iframe URLs.

## Open Product Decisions

1. OCR engine:
   - Tesseract in Docker;
   - Google Vision;
   - AWS Textract;
   - Azure Document Intelligence.
2. Rewriting:
   - deterministic templates only;
   - LLM-assisted with reviewer approval.
3. Review workflow:
   - internal admin only;
   - role-based reviewers later.
4. Deployment:
   - VPS with Docker Compose;
   - managed Postgres plus app server.
